"""Execute against a workspace bind mount on a local Docker daemon."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import subprocess

from .python_backend import DockerPythonBackend


class WorkspaceDocker:
    def __init__(self):
        self.backend = DockerPythonBackend()

    def profile(self):
        host = os.getenv('DOCKER_HOST')
        if not host:
            host = subprocess.check_output(
                ['docker','context','inspect','--format','{{.Endpoints.docker.Host}}'],
                text=True, timeout=10).strip()
        if not host.startswith('unix:///') or not Path(host.removeprefix('unix://')).is_socket():
            raise ValueError('就地挂载要求本地 Docker socket')
        return dict(self.backend.profile(),docker_host=host)

    async def run(self,eid,profile,args,env,cwd,readable,writable):
        host = profile['docker_host']
        docker = ['--host',host]
        name = 'artorias-workspace-'+eid
        cmd = [*docker,'create','--name',name,'--entrypoint',args[0],
               '--read-only','--network=none','--cap-drop=ALL','--security-opt=no-new-privileges',
               '--user',f'{os.getuid()}:{os.getgid()}','--memory=256m','--memory-swap=256m',
               '--cpus=1','--pids-limit=64','--log-driver=none','--tmpfs','/tmp:rw,nosuid,size=32m',
               '--workdir',str(cwd)]
        def mount(source,target,ro):
            if any(c in str(source)+str(target) for c in (',','"', '\n')):
                raise ValueError('Docker 挂载路径包含不支持的字符')
            cmd.extend(['--mount',f'type=bind,src={source},dst={target}'+(',readonly' if ro else '')])
        for path in dict.fromkeys([*readable,*writable]):
            mount(path,path,path not in writable)
        for path in cwd.iterdir():
            if path.name == '.git' or path.name in ('.ssh','.aws','.artorias','.env') or path.name.startswith('.env.'):
                if path.is_symlink():
                    raise ValueError('保护路径不能为链接')
                if path.name == '.git':
                    mount(path,path,True)
                elif path.is_dir():
                    cmd.extend(['--tmpfs',str(path)+':ro,noexec,nosuid,size=4k'])
                else:
                    mount('/dev/null',path,True)
        for key,value in env.items():
            cmd.extend(['-e',key+'='+value])
        cmd.extend([profile['image'],*args[1:]])
        proc = None
        try:
            await self.backend.command(cmd)
            proc = await asyncio.create_subprocess_exec('docker',*docker,'start','--attach',name,
                      stdin=asyncio.subprocess.DEVNULL,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            result = await collect(proc)
            state = json.loads(await self.backend.command([*docker,'inspect','--format','{{json .State}}',name]))
            return state['ExitCode'] or result[0],*result[1:]
        finally:
            if proc and proc.returncode is None:
                proc.kill()
                await proc.wait()
            with contextlib.suppress(ValueError):
                await asyncio.shield(self.backend.command([*docker,'rm','-f',name]))


async def collect(proc):
    async def tail(stream):
        data = bytearray()
        truncated = False
        while chunk := await stream.read(8192):
            data.extend(chunk)
            if len(data)>65536:
                del data[:-65536]
                truncated=True
        return data.decode(errors='replace'), truncated
    out, err = await asyncio.gather(tail(proc.stdout), tail(proc.stderr))
    return await proc.wait(), out[0], err[0], out[1] or err[1]
