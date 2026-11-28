"""Execute a workspace job on the machine that owns its files."""
import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
import subprocess
import tempfile
import time

from artorias_shared.execution.models import CommandRequest, safe_relative
from artorias_shared.execution.workspace import WorkspaceJob, digest
from .workspace_docker import WorkspaceDocker, collect
from .macos_python import MacOSVenvPythonBackend, sandbox_profile, PACKAGE, ProcessTimeout, StageFailure, redact_diagnostics


def within(root, relative):
    path = root / relative
    if path.resolve(strict=True) != path or not path.is_relative_to(root):
        raise ValueError('目录越界或包含符号链接')
    return path


def protected(relative):
    return any(part in ('.env','.ssh','.aws','.artorias') or part.startswith('.env.')
               for part in Path(relative).parts)


def read_input(root, relative, limit):
    """Open each component relative to a pinned directory; never follow links or block on a FIFO."""
    relative = safe_relative(str(relative))
    if protected(relative):
        raise ValueError('不能暂存凭据或应用配置文件')
    with contextlib.ExitStack() as stack:
        flags = os.O_RDONLY | os.O_NOFOLLOW
        fd = os.open(root, flags | os.O_DIRECTORY)
        stack.callback(os.close,fd)
        parts = Path(relative).parts
        for part in parts[:-1]:
            fd = os.open(part,flags | os.O_DIRECTORY,dir_fd=fd)
            stack.callback(os.close,fd)
        file = os.open(parts[-1],flags | os.O_NONBLOCK,dir_fd=fd)
        stack.callback(os.close,file)
        info = os.fstat(file)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError('输入文件类型或大小超出限制')
        with os.fdopen(file,'rb',closefd=False) as handle:
            data = handle.read(limit+1)
        if len(data)>limit:
            raise ValueError('输入文件超出限制')
        return data


def stage_inputs(root, items, destination=None):
    manifest = {}
    remaining = 32_000_000
    for item in items:
        source = within(root,safe_relative(item.resource))
        if source.is_dir() != item.directory:
            raise ValueError('输入类型不匹配')
        for file in source.rglob('*') if item.directory else [source]:
            if file.is_symlink():
                raise ValueError('不能暂存链接')
            if file.is_dir():
                continue
            target = Path(item.mount_path)
            if item.directory:
                target /= file.relative_to(source)
            if str(target) in manifest or len(manifest)>=500:
                raise ValueError('输入映射重复或超过数量限制')
            data = read_input(root,file.relative_to(root),remaining)
            remaining -= len(data)
            manifest[str(target)] = hashlib.sha256(data).hexdigest()
            if destination:
                dest = destination / target
                dest.parent.mkdir(parents=True,exist_ok=True)
                dest.write_bytes(data)
    return digest(manifest)


def command_arguments(argv):
    name, *args = argv
    if name == 'git' and args in (['status'], ['status', '--short'], ['status', '--porcelain']):
        return ['git', '--no-pager', '--no-optional-locks', '-c', 'core.fsmonitor=false',
                '-c', 'core.untrackedCache=false', '-c', 'core.hooksPath=/dev/null',
                *args, '--ignore-submodules=all']
    if name == 'rg' and all(not a.startswith('-') or a in
                            ('-n', '-i', '-l', '-F', '--files', '--hidden', '--') for a in args):
        return ['rg', '--no-config', *args]
    if name == 'ruff' and args and args[0] == 'format' and all(
            not a.startswith('-') or a == '--check' for a in args[1:]):
        return ['ruff', 'format', '--isolated', '--no-cache', *args[1:]]
    raise ValueError('支持 rg 搜索、git status [--short|--porcelain] 和 ruff format [--check]')


class WorkspaceRunner:
    def __init__(self, data, resolve_workspace, *, staging=None):
        self.data = Path(data).resolve()
        self.data.mkdir(parents=True, exist_ok=True)
        self.resolve_workspace = resolve_workspace
        self.staging = staging
        choice = os.getenv('ARTORIAS_WORKSPACE_SANDBOX','auto')
        if choice not in ('auto','docker','seatbelt','bubblewrap'):
            raise ValueError('未知的 workspace 沙箱')
        if choice == 'auto':
            choice = ('seatbelt' if sys.platform=='darwin' else
                      'bubblewrap' if shutil.which('bwrap') else 'docker')
        self.sandbox = choice
        self.docker = WorkspaceDocker()
        self.native = MacOSVenvPythonBackend(self.data / 'venvs')
        self.active = {}
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, spec TEXT, plan TEXT, status TEXT, result TEXT)')

    @contextlib.contextmanager
    def db(self):
        with contextlib.closing(sqlite3.connect(self.data / 'runner.sqlite')) as db, db:
            yield db

    def capabilities(self):
        live = {
            'seatbelt': sys.platform=='darwin' and Path('/usr/bin/sandbox-exec').is_file(),
            'bubblewrap': sys.platform.startswith('linux') and bool(shutil.which('bwrap')),
            'docker': bool(shutil.which('docker')),
        }[self.sandbox]
        return dict(commands=[name for name in ('rg','git','ruff') if live and (self.sandbox=='docker' or shutil.which(name))],
                    live_modes=['workspace_ro','workspace_rw'] if live else [],
                    staged_python=bool(self.staging))

    def describe(self, job):
        req = job.request
        root = Path(self.resolve_workspace(job)).resolve(strict=True)
        cwd = within(root, req.working_directory or '.')
        if protected(cwd.relative_to(root)) or '.git' in cwd.relative_to(root).parts:
            raise ValueError('不能将保护目录作为执行根')
        if not cwd.is_dir() or self.data.is_relative_to(root) or root.is_relative_to(self.data):
            raise ValueError('执行目录不可用或与执行器数据重叠')
        st = cwd.stat()
        binding = dict(policy_version=1, directory=str(cwd), identity=[st.st_dev, st.st_ino], mode=req.mode)
        if not isinstance(req,CommandRequest) and req.mode in ('workspace_copy','isolated'):
            if self.staging is None:
                raise ValueError('未配置暂存执行器')
            return dict(binding, **self.staging.describe(job,root))
        if isinstance(req, CommandRequest):
            if req.inputs:
                binding['inputs_hash'] = stage_inputs(cwd,req.inputs)
            args = command_arguments(req.argv)
            if self.sandbox == 'docker':
                return dict(binding,backend='docker',docker=self.docker.profile(),executable=args[0])
            exe = shutil.which(args[0])
            if not exe:
                raise ValueError(f'设备未安装 {args[0]}')
            if exe == '/usr/bin/git' and sys.platform == 'darwin':
                # The Apple launcher consults xcode-select and spawns the real Git.
                exe = subprocess.check_output(['/usr/bin/xcrun','--find','git'],text=True,
                                              timeout=5,env={'PATH':'/usr/bin:/bin'}).strip()
            exe = Path(exe).resolve(strict=True)
            if exe.is_relative_to(root):
                raise ValueError('命令行程序必须安装在 workspace 外')
            st = exe.stat()
            binding.update(executable=str(exe), revision=[st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size])
        else:
            if req.path:
                entry = within(cwd, req.path)
                if entry.suffix != '.py' or not entry.is_file():
                    raise ValueError('入口必须是 Python 文件')
                code = read_input(cwd,req.path,1_000_000)
            else:
                code = req.code.encode()
            binding['script_sha256'] = hashlib.sha256(code).hexdigest()
            if req.dependencies and (sys.platform != 'darwin' or any(not PACKAGE.fullmatch(x) for x in req.dependencies)):
                raise ValueError('该环境不支持请求的依赖')
        if self.sandbox == 'docker':
            if getattr(req,'dependencies',None):
                raise ValueError('Docker 就地执行使用镜像预装依赖')
            return dict(binding,backend='docker',docker=self.docker.profile(),executable='python')
        if self.sandbox == 'seatbelt' and sys.platform == 'darwin':
            binding['backend'] = 'seatbelt'
            if not isinstance(req, CommandRequest):
                binding['environment'] = self.native.profile()
        elif self.sandbox=='bubblewrap' and sys.platform.startswith('linux') and shutil.which('bwrap'):
            binding['backend'] = 'bubblewrap'
            if not isinstance(req, CommandRequest):
                python = Path('/usr/bin/python3').resolve(strict=True)
                revision = python.stat()
                binding.update(executable=str(python),revision=[revision.st_dev,revision.st_ino,
                                                               revision.st_mtime_ns,revision.st_size])
        else:
            raise ValueError('目标执行器需要 macOS Seatbelt 或 Linux bubblewrap')
        return binding

    async def prepare(self, job):
        spec = job.model_dump_json(exclude_unset=True)
        try:
            previous,raw,_,_ = self.row(job.execution_id)
        except KeyError:
            pass
        else:
            if json.loads(previous) != json.loads(spec):
                raise ValueError('同一执行 ID 的请求发生变化')
            return json.loads(raw)
        binding = self.describe(job)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT spec,plan FROM jobs WHERE id=?', (job.execution_id,)).fetchone()
            if row:
                if json.loads(row[0]) != json.loads(spec):
                    raise ValueError('同一执行 ID 的请求发生变化')
                return json.loads(row[1])
            plan = dict(binding, expires=time.time() + 3600)
            plan['plan_hash'] = digest([json.loads(spec), plan])
            db.execute('INSERT INTO jobs VALUES(?,?,?, ?,NULL)',
                       (job.execution_id, spec, json.dumps(plan), 'prepared'))
        return plan

    def row(self, eid):
        with self.db() as db:
            row = db.execute('SELECT spec,plan,status,result FROM jobs WHERE id=?', (eid,)).fetchone()
        if row is None:
            raise KeyError(eid)
        return row

    async def status(self, eid):
        _, raw, state, result = self.row(eid)
        if state == 'running' and eid not in self.active:
            state = 'unknown'
        if result:
            return json.loads(result)
        mode = json.loads(raw)['mode']
        return dict(execution_id=eid, execution_status=state, mode=mode,
                    effects='workspace_may_have_changed' if state=='unknown' and mode=='workspace_rw' else 'none',
                    publish_status='not_requested')

    async def start(self, eid, approval_hash):
        spec, raw, state, _ = self.row(eid)
        plan = json.loads(raw)
        if plan['plan_hash'] != approval_hash:
            raise ValueError('批准的执行计划不匹配')
        if state == 'prepared':
            job = WorkspaceJob.model_validate_json(spec)
            if plan['expires'] < time.time() or self.describe(job) != {
                    k:v for k,v in plan.items() if k not in ('expires','plan_hash')}:
                raise ValueError('执行计划已过期或 workspace 已变化')
            with self.db() as db:
                claimed = db.execute("UPDATE jobs SET status='running' WHERE id=? AND status='prepared'", (eid,)).rowcount
            if claimed:
                self.active[eid] = asyncio.create_task(self.run(job, plan))
        return await self.status(eid)

    async def cancel(self, eid):
        with self.db() as db:
            db.execute("UPDATE jobs SET status='cancelled' WHERE id=? AND status='prepared'", (eid,))
        task = self.active.get(eid)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if task.cancelled():
                self.finish(eid, {'execution_status':'cancelled'})
        return await self.status(eid)

    def finish(self, eid, result):
        with self.db() as db:
            db.execute('UPDATE jobs SET status=?,result=? WHERE id=?',
                       (result['execution_status'],json.dumps(result),eid))
        self.active.pop(eid, None)

    async def run(self, job, plan):
        req = job.request
        eid = job.execution_id
        effects = 'none'
        try:
            lock_path = self.data / ('lock-' + digest(str(Path(self.resolve_workspace(job)).resolve())))
            with lock_path.open('a') as lock, tempfile.TemporaryDirectory(dir=self.data) as tmp:
                flag = fcntl.LOCK_EX if req.mode == 'workspace_rw' else fcntl.LOCK_SH
                async with asyncio.timeout(req.timeout_seconds):
                    while True:
                        try:
                            fcntl.flock(lock, flag | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            await asyncio.sleep(.1)
                    binding = self.describe(job)
                    if any(plan.get(k) != v for k,v in binding.items()):
                        raise ValueError('等待执行期间 workspace 或入口发生变化')
                    effects = 'workspace_may_have_changed' if req.mode == 'workspace_rw' else 'none'
                    result = await self.launch(job, plan, Path(tmp))
        except asyncio.CancelledError:
            result = dict(execution_status='cancelled')
        except StageFailure as exc:
            result = exc.result
        except ProcessTimeout as exc:
            result = dict(execution_status='timed_out',stdout_tail=exc.stdout,stderr_tail=exc.stderr,
                          logs_truncated=exc.truncated)
        except TimeoutError:
            result = dict(execution_status='timed_out')
        except Exception as exc:
            result = dict(execution_status='failed', error=str(exc)[:2000])
        for key in ('stdout_tail','stderr_tail'):
            if key in result:
                result[key] = redact_diagnostics(result[key])
        result.setdefault('effects',effects)
        result.setdefault('publish_status','not_requested')
        self.finish(eid, dict(result, execution_id=eid, mode=req.mode, working_directory=plan['directory']))

    async def launch(self, job, plan, tmp):
        req = job.request
        if 'staged_id' in plan:
            return await self.staging.execute(job,self.resolve_workspace(job))
        directory = Path(plan['directory'])
        scratch = tmp / 'scratch'
        scratch.mkdir()
        work = directory if req.mode != 'isolated' else scratch
        env = dict(HOME=str(scratch), TMPDIR=str(scratch), PATH='/usr/bin:/bin',
                   ARTORIAS_WORK_DIR=str(work), GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null',
                   GIT_TERMINAL_PROMPT='0')
        readable = [work, scratch]
        writable = [scratch] + ([work] if req.mode == 'workspace_rw' else [])
        if isinstance(req, CommandRequest):
            args = command_arguments(req.argv)
            exe = Path(plan['executable'])
            args[0] = str(exe)
            if req.inputs:
                inputs = tmp / 'inputs'
                inputs.mkdir()
                if stage_inputs(directory,req.inputs,inputs) != plan['inputs_hash']:
                    raise ValueError('审批后的输入发生变化')
                readable.append(inputs)
                args = [str(inputs / safe_relative(a[8:])) if a.startswith('/inputs/') else a for a in args]
        else:
            script = tmp / 'entry.py'
            entry = within(directory, req.path) if req.path else None
            code = read_input(directory,req.path,1_000_000) if entry else req.code.encode()
            if hashlib.sha256(code).hexdigest() != plan['script_sha256']:
                raise ValueError('入口内容与批准版本不一致')
            script.write_bytes(code)
            readable.append(script)
            if plan['backend']=='seatbelt':
                venv = await self.native._environment(job.execution_id, plan['environment'], req.dependencies)
                exe = venv / 'bin/python'
                readable.extend([venv, plan['environment']['python_prefix']])
            else:
                exe = Path(plan['executable'])
            env['ARTORIAS_IMPORT_DIR'] = str(entry.parent if entry else work)
            loader = "import os,runpy,sys;sys.path[:0]=[os.environ['ARTORIAS_IMPORT_DIR'],os.getcwd()];sys.argv=sys.argv[1:];runpy.run_path(sys.argv[0],run_name='__main__')"
            args = [str(exe), '-I', '-B', '-c', loader, str(script), *req.args]
        if plan['backend']=='docker':
            env['PATH'] = '/usr/local/bin:/usr/bin:/bin'
            code,out,err,truncated = await self.docker.run(job.execution_id,plan['docker'],args,env,
                                                           work,readable,writable)
        elif plan['backend']=='seatbelt':
            readable.append(exe)
            policy = sandbox_profile(readable, writable, [exe], bound_paths=(work,))
            policy += '\n(deny file-write* (regex #"/[.]git(/|$)" ) (regex #"/[.]artorias(/|$)"))'
            policy += '\n(deny file-read* file-write* (regex #"/([.]env([.][^/]*)?|[.]ssh|[.]aws)(/|$)"))'
            code, out, err, truncated = await self.native._run_process(job.execution_id, args, policy=policy,
                cwd=work, env=env, timeout=req.timeout_seconds)
        else:
            code, out, err, truncated = await linux_run(args, work, env, [*readable,exe], writable)
        return dict(execution_status='succeeded' if code == 0 else 'failed', exit_code=code,
                    stdout_tail=out, stderr_tail=err, logs_truncated=truncated)


async def linux_run(args, cwd, env, readable, writable):
    """Private PID/network/mount namespaces; subprocesses die with the namespace."""
    cmd = ['bwrap', '--die-with-parent', '--unshare-all', '--new-session', '--cap-drop', 'ALL',
           '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp']
    for base in ('/usr','/bin','/lib','/lib64','/etc/ld.so.cache'):
        path = Path(base)
        if path.is_symlink():
            cmd.extend(['--symlink', os.readlink(path), base])
        elif path.exists():
            cmd.extend(['--ro-bind', base, base])
    for path in map(Path, readable):
        if path not in writable and not path.is_relative_to('/usr'):
            cmd.extend(['--ro-bind',str(path),str(path)])
    for path in writable:
        cmd.extend(['--bind',str(path),str(path)])
    # Protect existing metadata in the exposed workspace. The container has no host HOME.
    for name in ('.git','.env','.aws','.ssh','.artorias'):
        path = cwd / name
        if path.is_symlink():
            raise ValueError('保护路径不能为链接')
        if path.exists():
            if name == '.git':
                cmd.extend(['--ro-bind', str(path), str(path)])
            elif path.is_dir():
                cmd.extend(['--tmpfs', str(path), '--remount-ro', str(path)])
            else:
                cmd.extend(['--ro-bind','/dev/null',str(path)])
    cmd.extend(['--chdir',str(cwd),'--',*args])
    proc = await asyncio.create_subprocess_exec(*cmd, env=env, stdin=asyncio.subprocess.DEVNULL,
              stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        return await collect(proc)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
