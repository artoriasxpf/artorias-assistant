"""Isolated local Docker backend. No host-writable mount is given to Python."""
import asyncio
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tarfile
from artorias_shared.execution.models import safe_relative

BOOTSTRAP = '''import os,sys,shutil,runpy
if os.environ['ARTORIAS_MODE']=='workspace_copy':
    shutil.copytree('/seed','/work',dirs_exist_ok=True)
    for directory,dirs,files in os.walk('/work'):
        os.chmod(directory,0o755)
        for name in files:os.chmod(os.path.join(directory,name),0o644)
os.chdir('/work')
sys.path[:0]=['/work','/app']
sys.argv=['/app/__entry__.py']+sys.argv[1:]
runpy.run_path('/app/__entry__.py',run_name='__main__')
'''


# Read tmpfs from inside the namespace (docker cp cannot reliably archive tmpfs).
# The script has exited; terminate remaining same-UID descendants before collecting.
COLLECT = r'''import os,sys,signal,time,stat,io,tarfile
from pathlib import Path
def live(p):
    if not p.name.isdigit() or int(p.name) in (1,os.getpid()):return False
    try:return (p/'stat').read_text().rsplit(')',1)[1].split()[0] not in ('Z','X')
    except FileNotFoundError:return False
for attempt in range(100):
    others=[int(p.name) for p in Path('/proc').iterdir() if live(p)]
    if not others:break
    for pid in others:
        try:os.kill(pid,signal.SIGKILL)
        except ProcessLookupError:pass
    time.sleep(.02)
else:raise RuntimeError('Cannot quiesce sandbox processes')
roots=[('/outputs','outputs')]
if sys.argv[1]=='1':roots.append(('/work','work_result'))
total=0;count=0
with tarfile.open(fileobj=sys.stdout.buffer,mode='w|') as archive:
    for root,prefix in roots:
        for directory,dirs,files in os.walk(root,followlinks=False):
            for name in dirs+files:
                path=Path(directory)/name;info=path.lstat()
                if stat.S_ISDIR(info.st_mode):continue
                if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>32000000:raise ValueError('Unsafe artifact')
                count+=1;total+=info.st_size
                if count>500 or total>96000000:raise ValueError('Artifact quota exceeded')
                fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
                with os.fdopen(fd,'rb') as source:data=source.read(32000001)
                if len(data)!=info.st_size:raise ValueError('Artifact changed')
                item=tarfile.TarInfo(prefix+'/'+path.relative_to(root).as_posix());item.size=len(data);item.mode=0o600
                archive.addfile(item,io.BytesIO(data))
'''


def unpack(data, destination):
    destination=Path(destination);destination.mkdir(parents=True,exist_ok=True)
    seen=set();total=0;count=0
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:*') as archive:
        for member in archive:
            if member.name in ('.','./'):continue
            name=safe_relative(member.name)
            if name in seen:raise ValueError('产物路径重复')
            seen.add(name)
            if member.isdir():continue
            if not member.isfile() or member.size>32_000_000:raise ValueError('产物包含链接、特殊文件或超大文件')
            total+=member.size;count+=1
            if total>96_000_000 or count>500:raise ValueError('产物超过配额')
            target=destination/name;target.parent.mkdir(parents=True,exist_ok=True)
            raw=archive.extractfile(member).read(member.size+1)
            if len(raw)!=member.size:raise ValueError('产物大小不匹配')
            with target.open('xb') as out:out.write(raw)


class DockerPythonBackend:
    def __init__(self, *, location='device'):
        if location not in ('device', 'cloud'):
            raise ValueError('Python 执行位置必须是 device 或 cloud')
        self.location=location

    def profile(self):
        image=os.getenv('ARTORIAS_IMAGE','python:3.12-slim')
        try:
            proc=subprocess.run(['docker','image','inspect',image,'--format','{{.Id}}'],capture_output=True,text=True,timeout=10,check=True)
        except (OSError,subprocess.SubprocessError) as exc:
            raise ValueError('Docker 或预装 Python 镜像不可用；请启动 Docker 并预先拉取镜像') from exc
        digest=proc.stdout.strip()
        if not digest.startswith('sha256:') or len(digest)!=71:raise ValueError('无法确定镜像内容 ID')
        return {'id':'python-default','image':digest,'location':self.location,'memory_mb':256,'cpus':1,'pids':64,'output_bytes':64_000_000,'work_bytes':128_000_000}

    async def command(self,args,limit=128_000,timeout=15):
        proc=await asyncio.create_subprocess_exec('docker',*args,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        async def read(stream,cap):
            data=bytearray()
            while chunk:=await stream.read(65536):
                data.extend(chunk)
                if len(data)>cap:raise ValueError('Docker 输出超出配额')
            return bytes(data)
        try:
            async def complete():
                stdout,stderr=await asyncio.gather(read(proc.stdout,limit),read(proc.stderr,65536))
                await proc.wait()
                return stdout,stderr
            stdout,stderr=await asyncio.wait_for(complete(),timeout)
            if proc.returncode:raise ValueError('Docker 操作失败：'+stderr.decode(errors='replace')[-2000:])
            return stdout
        finally:
            if proc.returncode is None:proc.kill();await proc.wait()

    async def stop(self, execution_id):
        await self.command(['rm','-f','artorias-'+execution_id],timeout=15)

    async def run(self,plan,root):
        req=plan['request'];name='artorias-'+plan['id'];created=False
        logs=root/'logs.txt';status='failed';exit_code=None;truncated=False
        for folder in ('app','inputs','seed'):
            for directory,dirs,files in os.walk(root/folder):
                os.chmod(directory,0o755)
                for f in files:os.chmod(Path(directory)/f,0o444)
        args=['create','--entrypoint=python','--name',name,'--network=none','--memory=256m','--memory-swap=256m','--cpus=1','--pids-limit=64',
              '--read-only','--cap-drop=ALL','--security-opt=no-new-privileges','--user=65534:65534',
              '--log-driver=none','--tmpfs','/tmp:rw,noexec,nosuid,size=32m,mode=1777',
              '--tmpfs','/work:rw,nosuid,size=128m,mode=1777','--tmpfs','/outputs:rw,noexec,nosuid,size=64m,mode=1777','-w','/work']
        for folder,target in (('app','/app'),('inputs','/inputs'),('seed','/seed')):
            args+=['--mount',f'type=bind,src={root/folder},dst={target},readonly']
        for key,value in {'ARTORIAS_MODE':req['mode'],'ARTORIAS_INPUT_DIR':'/inputs','ARTORIAS_OUTPUT_DIR':'/outputs','ARTORIAS_WORK_DIR':'/work','PYTHONDONTWRITEBYTECODE':'1'}.items():args+=['-e',key+'='+value]
        args += [plan['profile']['image'],'-c','import time; time.sleep(3600)']
        try:
            await self.command(args);created=True
            await self.command(['start',name])
            proc=await asyncio.create_subprocess_exec('docker','exec',name,'python','-B','-c',BOOTSTRAP,*req['args'],stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            async def tail(stream):
                nonlocal truncated
                data=bytearray()
                while chunk:=await stream.read(8192):
                    data.extend(chunk)
                    if len(data)>65536:
                        truncated=True;del data[:-65536]
                return data.decode(errors='replace')
            try:
                async def complete():
                    tails=await asyncio.gather(tail(proc.stdout),tail(proc.stderr))
                    code=await proc.wait()
                    return tails,code
                tails,exit_code=await asyncio.wait_for(complete(),req['timeout_seconds'])
                status='succeeded' if exit_code==0 else 'failed'
            except TimeoutError:
                await self.command(['kill',name])
                return {'execution_status':'timed_out','exit_code':None,'stdout_tail':'','stderr_tail':'运行超时，未收集产物','logs_ref':None}
            finally:
                if proc.returncode is None:proc.kill();await proc.wait()
            logs.write_text('STDOUT\n'+tails[0]+'\nSTDERR\n'+tails[1])
            state=json.loads((await self.command(['inspect',name,'--format','{{json .State}}'])).decode())
            if state['Running']:
                raw=await self.command(['exec',name,'python','-B','-c',COLLECT,
                                        '1' if req['mode']=='workspace_copy' else '0'],limit=110_000_000,timeout=30)
                # Archive paths are outputs/* and work_result/*, validated before writing.
                collection=root/'collection';unpack(raw,collection)
                import shutil
                for folder in ('outputs','work_result'):
                    if (collection/folder).exists():shutil.copytree(collection/folder,root/folder,dirs_exist_ok=True)
                shutil.rmtree(collection,ignore_errors=True)
            else:
                status='failed'
                tails=['','容器异常退出；可能超过内存或临时存储配额']
            return {'execution_status':status,'exit_code':exit_code,'stdout_tail':tails[0],'stderr_tail':tails[1],
                    'logs_truncated':truncated,'logs_ref':plan['id']+'/logs.txt'}
        finally:
            if created:
                try:await asyncio.shield(self.command(['rm','-f',name],timeout=15))
                except Exception:pass  # durable execution ID names the container for reconciliation
