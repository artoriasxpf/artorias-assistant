"""Run staged Python code under a deny-by-default macOS Seatbelt profile."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import tempfile


SEATBELT = '/usr/bin/sandbox-exec'
PACKAGE = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9._-]*(?:==[a-zA-Z0-9][a-zA-Z0-9.!+_-]*)?')
BOOTSTRAP = """
import errno, os, resource, runpy, socket, sys
resource.setrlimit(resource.RLIMIT_FSIZE, (32_000_000, 32_000_000))
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
work = os.environ['ARTORIAS_WORK_DIR']
script = sys.argv[1]
try:
    fd = os.open(script, os.O_WRONLY)
except PermissionError:
    pass
else:
    os.close(fd)
    raise RuntimeError('Seatbelt did not protect staged code')
try:
    with socket.socket() as probe:
        result = probe.connect_ex(('127.0.0.1', 1))
except PermissionError as error:
    result = error.errno
if result not in (errno.EACCES, errno.EPERM):
    raise RuntimeError('Seatbelt did not block outbound sockets')
sys.path[:0] = [work, os.path.dirname(script)]
sys.argv = sys.argv[1:]
runpy.run_path(script, run_name='__main__')
"""


def sandbox_profile(readable, writable, executable, *, install=False, fork=False):
    def paths(rule, roots, match='subpath'):
        return [
            f'(allow {rule} ({match} {json.dumps(str(Path(root).resolve()), ensure_ascii=False)}))'
            for root in roots
        ]

    ancestors = {str(parent) for root in [*readable, *writable, *executable]
                 for parent in Path(root).resolve().parents}
    return '\n'.join([
        '(version 1)', '(deny default)', '(allow sysctl-read)',
        *paths('file-read*', ['/System/Library', '/usr/lib', '/usr/share/locale', *readable]),
        *paths('file-read*', ['/dev/null', '/dev/urandom', '/dev/random', '/private/etc/localtime'], 'literal'),
        *paths('file-write-data', ['/dev/null'], 'literal'),
        *paths('file-read-metadata', sorted(ancestors), 'literal'),
        *paths('file-write*', writable),
        *paths('process-exec', executable, 'literal'),
        *(['(allow process-fork)'] if fork else []),
        *(['(allow network-outbound)',
           *paths('file-read*', ['/private/etc/resolv.conf', '/private/etc/hosts'], 'literal'),
           '(allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo") (global-name "com.apple.mDNSResponder"))']
          if install else []),
    ])


class MacOSVenvPythonBackend:
    location = 'device'
    native = True

    def __init__(self, cache):
        self.cache = Path(cache).resolve()
        self.active = {}
        self.environment_lock = asyncio.Lock()

    def profile(self):
        uv = shutil.which('uv')
        if sys.platform != 'darwin' or not Path(SEATBELT).is_file() or not uv:
            raise ValueError('原生 Python 后端需要 macOS、sandbox-exec 和 uv')
        python = Path(sys._base_executable).resolve()
        info = python.stat()
        uv_binary = Path(uv).resolve()
        uv_info = uv_binary.stat()
        return {
            'id': 'python-default', 'backend': 'macos-venv', 'location': self.location,
            'python': str(python), 'python_revision': [info.st_size, info.st_mtime_ns],
            'python_prefix': str(Path(sys.base_prefix).resolve()),
            'python_version': sys.version, 'architecture': os.uname().machine,
            'uv': str(uv_binary), 'uv_revision': [uv_info.st_size, uv_info.st_mtime_ns],
        }

    async def _run_process(self, execution_id, args, *, policy, cwd, env, timeout):
        proc = await asyncio.create_subprocess_exec(
            SEATBELT, '-p', policy, *map(str, args), cwd=cwd, env=env,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True,
        )
        self.active[execution_id] = proc.pid
        truncated = False

        async def tail(stream):
            nonlocal truncated
            data = bytearray()
            while chunk := await stream.read(8192):
                data.extend(chunk)
                if len(data) > 65536:
                    truncated = True
                    del data[:-65536]
            return data.decode(errors='replace')

        try:
            stdout, stderr = await asyncio.wait_for(
                asyncio.gather(tail(proc.stdout), tail(proc.stderr)), timeout,
            )
            code = await asyncio.wait_for(proc.wait(), 2)
            return code, stdout, stderr, truncated
        finally:
            try:
                await self.stop(execution_id)
            finally:
                self.active.pop(execution_id, None)
                await proc.wait()

    async def stop(self, execution_id):
        pid = self.active.get(execution_id)
        if pid is not None:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    async def _environment(self, execution_id, profile, dependencies):
        identity = json.dumps([profile, sorted(set(dependencies))], sort_keys=True)
        key = hashlib.sha256(identity.encode()).hexdigest()
        self.cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        async with self.environment_lock:
            # Use a unique final path for venv launchers with absolute shebangs.
            marker = self.cache / (key + '.json')
            if marker.exists():
                current = json.loads(marker.read_text())
                candidate = self.cache / current / 'venv'
                if (candidate / 'bin/python').is_file():
                    return candidate
            base = Path(tempfile.mkdtemp(prefix=key + '-', dir=self.cache))
            env = base / 'venv'
            temporary = base / 'tmp'
            temporary.mkdir()
            python = profile['python']
            uv = profile['uv']
            req = base / 'requirements.in'
            req.write_text('\n'.join(sorted(set(dependencies))) + '\n')
            lock = base / 'requirements.lock'
            policy = sandbox_profile(
                [base, python, uv, profile['python_prefix']], [base], [python, uv],
                install=bool(dependencies), fork=True,
            )
            install_env = {
                'PATH': '/usr/bin:/bin', 'HOME': str(base), 'TMPDIR': str(temporary),
                'UV_CACHE_DIR': str(base / 'cache'), 'UV_PYTHON_DOWNLOADS': 'never',
            }
            commands = [[uv, '--no-config', 'venv', '--python', python, env]]
            if dependencies:
                common = ['--no-build', '--index-url', 'https://pypi.org/simple']
                commands += [
                    [uv, '--no-config', 'pip', 'compile', req, '--python', python,
                     '--generate-hashes', '--output-file', lock, *common],
                    [uv, '--no-config', 'pip', 'sync', lock, '--python', env / 'bin/python',
                     '--require-hashes', '--link-mode', 'copy', *common],
                ]
            try:
                for command in commands:
                    code, _, stderr, _ = await self._run_process(
                        execution_id, command, policy=policy, cwd=base, env=install_env, timeout=120,
                    )
                    if code:
                        raise ValueError('Python 环境准备失败：' + stderr[-2000:])
                shutil.rmtree(base / 'cache', ignore_errors=True)
                pending = base / 'ready.json'
                pending.write_text(json.dumps(base.name))
                pending.replace(marker)
                return env
            except BaseException:
                shutil.rmtree(base, ignore_errors=True)
                raise

    async def run(self, plan, root):
        profile = self.profile()
        if plan['profile'] != profile:
            raise ValueError('执行环境已变化，请重新申请执行')
        request = plan['request']
        dependencies = request.get('dependencies', [])
        if any(not PACKAGE.fullmatch(package) for package in dependencies):
            raise ValueError('依赖必须是包名或 package==version')
        execution_id = plan['id']
        try:
            venv = await self._environment(execution_id, profile, dependencies)
            work = root / ('work_result' if request['mode'] == 'workspace_copy' else 'native_work')
            if request['mode'] == 'workspace_copy':
                shutil.copytree(root / 'seed', work, dirs_exist_ok=True)
            else:
                work.mkdir()
            temporary = root / 'native_tmp'
            temporary.mkdir()
            outputs = root / 'outputs'
            app = root / 'app'
            inputs = root / 'inputs'
            python = venv / 'bin/python'
            policy = sandbox_profile(
                [app, inputs, venv, profile['python_prefix'], python, work, outputs, temporary],
                [work, outputs, temporary], [python],
            )
            env = {
                'HOME': str(temporary), 'TMPDIR': str(temporary), 'PATH': '/usr/bin:/bin',
                'ARTORIAS_INPUT_DIR': str(inputs), 'ARTORIAS_OUTPUT_DIR': str(outputs),
                'ARTORIAS_WORK_DIR': str(work), 'ARTORIAS_MODE': request['mode'],
            }
            mappings = {'/inputs': inputs, '/outputs': outputs, '/work': work, '/app': app}
            args = []
            for arg in request['args']:
                prefix, _, suffix = arg[1:].partition('/') if arg.startswith('/') else ('', '', '')
                base = mappings.get('/' + prefix)
                args.append(str(base / suffix) if base is not None and '..' not in Path(suffix).parts
                            and not suffix.startswith('/') else arg)
            code, stdout, stderr, truncated = await self._run_process(
                execution_id, [python, '-I', '-B', '-c', BOOTSTRAP, app / '__entry__.py', *args],
                policy=policy, cwd=work, env=env, timeout=request['timeout_seconds'],
            )
            (root / 'logs.txt').write_text('STDOUT\n' + stdout + '\nSTDERR\n' + stderr)
            return {
                'execution_status': 'succeeded' if code == 0 else 'failed', 'exit_code': code,
                'stdout_tail': stdout, 'stderr_tail': stderr, 'logs_truncated': truncated,
                'logs_ref': execution_id + '/logs.txt',
            }
        except TimeoutError:
            for name in ('outputs', 'work_result'):
                shutil.rmtree(root / name, ignore_errors=True)
            return {'execution_status': 'timed_out', 'exit_code': None,
                    'stdout_tail': '', 'stderr_tail': '运行或环境准备超时', 'logs_ref': None}
        finally:
            for name in ('native_work', 'native_tmp'):
                shutil.rmtree(root / name, ignore_errors=True)
