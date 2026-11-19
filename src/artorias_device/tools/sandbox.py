"""Local Docker executor. Cloud implementations are injected via SandboxExecutor."""
import asyncio
import os
import tempfile
from pathlib import Path

class LocalDockerExecutor:
    async def run_python(self, ws, path):
        # Execution exports only relative files from the bound space.
        if Path(path).is_absolute() or '..' in Path(path).parts or '://' in path:raise ValueError('脚本必须位于默认空间内，使用相对路径')
        if path not in ws.files() or not path.endswith('.py'):raise ValueError('请选择已有 Python 文件')
        from artorias_shared.ids import uid
        name='artorias-'+uid()
        with tempfile.TemporaryDirectory() as d:
            for f in ws.files():
                p=Path(d)/f;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(ws.read_bytes(f))
            args=['docker','run','--rm','--name',name,'--network=none','--memory=256m','--cpus=1','--pids-limit=64','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges','--user=65534:65534','--tmpfs','/tmp:rw,noexec,nosuid,size=32m','-v',f'{d}:/work:ro','-w','/work',os.getenv('ARTORIAS_IMAGE','python:3.12-slim'),'python','-B',path]
            os.chmod(d,0o755)
            proc=None
            try:
                proc=await asyncio.create_subprocess_exec(*args,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT)
                data=bytearray()
                async def collect():
                    while chunk:=await proc.stdout.read(4096):
                        if len(data)+len(chunk)>65536:raise ValueError('输出超过 64KB')
                        data.extend(chunk)
                    return await proc.wait()
                code=await asyncio.wait_for(collect(),30)
                result={'exit_code':code,'output':data.decode(errors='replace')}
                return result
            finally:
                if proc is not None:
                    cleanup=await asyncio.create_subprocess_exec('docker','rm','-f',name,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
                    await cleanup.wait()
                    if proc.returncode is None:proc.kill();await proc.wait()
