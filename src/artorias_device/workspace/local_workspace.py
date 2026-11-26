"""Local working tree with immutable backups and a durable mutation journal."""
import hashlib
import os
import stat
import tempfile
import threading
from collections import deque
from contextlib import contextmanager
from pathlib import Path

from artorias_shared.ids import uid
from .blobs import FilesystemBlobStore
from artorias_shared.workspace import BlobRef, WorkspaceJournal, UNSET_VERSION

_UNSET = UNSET_VERSION
_LOCK = threading.RLock()


def digest(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


class LocalWorkspace:
    """The disk is authoritative. The first argument remains the backup directory."""

    def __init__(self, root, journal: WorkspaceJournal, session, run, workspace_root=None, *,
                 bind_session=True, create_root=True, resource_meta=None):
        self.blobs = Path(root).expanduser().resolve()
        self.blobs.mkdir(parents=True, exist_ok=True)
        self.journal, self.session, self.run = journal, session, run
        self.resource_meta = resource_meta
        bound = journal.binding(session) if bind_session else None
        requested = workspace_root or os.getenv('ARTORIAS_WORKSPACE')
        candidate = Path(requested).expanduser().resolve() if requested else self.blobs.parent/'workspaces'/session
        self.root = Path(bound) if bound else candidate
        data_root = Path(journal.data_root).resolve()
        if data_root == self.root or self.root in data_root.parents or self.root == self.blobs or self.blobs in self.root.parents:
            raise ValueError('工作目录不能包含应用数据目录，也不能位于 blobs 中')
        if bound and workspace_root and candidate != self.root:
            raise ValueError('会话已绑定其他工作目录')
        if not bound and bind_session:
            actual = journal.bind(session, str(self.root))
            if actual != str(self.root):
                raise ValueError('会话已绑定其他工作目录')
        if create_root:
            self.root.mkdir(parents=True, exist_ok=True)
        with self._locked():
            self._recover()

    @contextmanager
    def _locked(self):
        # Local macOS/Linux processes sharing this data directory serialize mutations.
        import fcntl
        with _LOCK, (self.blobs/'.workspace.lock').open('a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def path(self, path):
        if not isinstance(path, str) or not path or '\x00' in path:
            raise ValueError('文件路径不能为空')
        p = Path(path)
        if '..' in p.parts:
            raise ValueError('路径不能包含 ..')
        if p.is_absolute():
            try:
                p = p.relative_to(self.root)
            except ValueError:
                raise ValueError('路径超出工作目录') from None
        if not p.parts:
            raise ValueError('需要文件路径')
        target = self.root
        if self.root.resolve() != self.root:
            raise ValueError('工作目录已被替换为符号链接')
        for part in p.parts:
            target = target/part
            if target.is_symlink():
                raise ValueError('不允许访问符号链接')
        return p.as_posix()

    def _snapshot(self, path, limit=32_000_000):
        target = self.root/self.path(path)
        try:
            info = target.stat()
        except FileNotFoundError:
            return None, None
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('只支持普通文件')
        if info.st_size > limit:
            raise ValueError('文件超过大小限制')
        with target.open('rb') as f:
            data = f.read(limit + 1)
        if len(data) > limit:
            raise ValueError('文件超过大小限制')
        return data, stat.S_IMODE(info.st_mode)

    def read_bytes(self, path):
        data, _ = self._snapshot(path)
        if data is None:
            raise FileNotFoundError(path)
        return data

    def read_version(self, path):
        data, _ = self._snapshot(path, limit=1_000_000)
        if data is None:raise FileNotFoundError(path)
        return data.decode('utf-8'), digest(data)

    def read(self, path):
        return self.read_version(path)[0]

    def head(self, path):
        return digest(self._snapshot(path)[0])

    def files(self):
        return self.files_under('.')

    def _directory(self, directory):
        if directory == '.':
            if self.root.resolve() != self.root:
                raise ValueError('工作目录已被替换为符号链接')
            directory = self.root
        else:
            directory = self.root/self.path(directory)
        if not stat.S_ISDIR(directory.stat().st_mode):
            raise NotADirectoryError(f'不是目录：{directory}')
        return directory

    def list_directory(self, directory):
        return self.tree(directory, max_depth=1, max_entries=None, include_hidden=True)['entries']

    def tree(self, directory, *, max_depth, max_entries, include_hidden):
        root = self._directory(directory)
        pending = deque([(root, 0)])
        found = []
        truncated = False
        while pending and not truncated:
            current, depth = pending.popleft()
            if depth >= max_depth:
                continue
            if current != root:
                self.path(current.relative_to(self.root).as_posix())
            with os.scandir(current) as children:
                for child in children:
                    if not include_hidden and child.name.startswith('.'):
                        continue
                    if child.is_dir(follow_symlinks=False):
                        kind = 'directory'
                    elif child.is_file(follow_symlinks=False):
                        kind = 'file'
                    else:
                        continue
                    if max_entries is not None and len(found) == max_entries:
                        truncated = True
                        break
                    path = Path(child.path)
                    found.append({'path': path.relative_to(root).as_posix(), 'type': kind})
                    if kind == 'directory' and depth + 1 < max_depth:
                        pending.append((path, depth + 1))
        return {'entries': sorted(found, key=lambda item: item['path']), 'truncated': truncated}

    def files_under(self, directory):
        directory = self._directory(directory)

        def raise_walk_error(error):
            raise error

        result = []
        for current, dirs, files in os.walk(directory, followlinks=False, onerror=raise_walk_error):
            current = Path(current)
            dirs[:] = sorted(d for d in dirs if not (current/d).is_symlink())
            for name in sorted(files):
                p = current/name
                if not p.is_symlink() and p.is_file():
                    result.append(p.relative_to(directory).as_posix())
        return sorted(result)

    def analysis_entries(self, directory, *, max_depth, include_hidden):
        from .analysis import entries
        return entries(self, directory, max_depth=max_depth, include_hidden=include_hidden)

    def analysis_text(self, path, *, max_bytes, versioned):
        from .analysis import read_text
        return read_text(self, path, max_bytes=max_bytes, versioned=versioned)

    def analysis_search(self, path, **options):
        from .analysis import search_text
        return search_text(self, path, **options)

    def _backup(self, data):
        if data is None:
            return None
        return FilesystemBlobStore(self.blobs,self._atomic).put(data).sha256

    def _blob(self, h):
        if h is None:
            return None
        return FilesystemBlobStore(self.blobs,self._atomic).get(BlobRef(str(self.blobs),h))

    @staticmethod
    def _sync_dir(directory):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @classmethod
    def _atomic(cls, target, data, mode):
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='.artorias-', dir=target.parent)
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(data)
                os.fchmod(f.fileno(), mode)
                f.flush()
                os.fsync(f.fileno())
            os.replace(name, target)
            cls._sync_dir(target.parent)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _finish(self, op):
        self.journal.finish(op)

    def _recover(self):
        # Reconcile evidence; never blindly replay an uncertain filesystem action.
        for op in self.journal.pending(str(self.root)):
            data, mode = self._snapshot(op['path'])
            h = digest(data)
            if h == op['after_hash'] and mode == op['after_mode']:
                self._finish(op)
            elif h == op['before_hash'] and mode == op['before_mode']:
                self.journal.abort(op['id'])
            else:
                # Keep pending so further mutation cannot obscure recovery evidence.
                continue

    def _mutate(self, path, data, expected=_UNSET, reverts=None, mode=None, kind='file.changed', operation_id=None):
        with self._locked():
            path = self.path(path)
            self._recover()
            if operation_id:
                previous = self.journal.committed(operation_id, self.session, str(self.root))
                if previous:
                    if previous['path'] != path or previous['after_hash'] != digest(data) or (expected is not _UNSET and previous['before_hash'] != expected):
                        raise ValueError('幂等操作参数不一致')
                    return {'change_id':previous['id'], 'path':path, 'version':previous['after_hash']}
            if any(Path(op['root'])/op['path'] == self.root/path for op in self.journal.pending()):
                raise ValueError('存在未解决的中断操作；请核对备份和磁盘文件后再操作')
            before, before_mode = self._snapshot(path)
            if expected is not _UNSET and digest(before) != expected:
                raise ValueError('版本冲突：磁盘文件已修改')
            after_mode = (mode if mode is not None else before_mode if before_mode is not None else 0o644) if data is not None else None
            op = dict(id=operation_id or uid(), root=str(self.root), session=self.session, run=self.run,
                      path=path, before_hash=self._backup(before), after_hash=self._backup(data),
                      before_mode=before_mode, after_mode=after_mode, reverts=reverts, kind=kind, status='pending')
            resource = dict(self.resource_meta, blob_root=str(self.blobs)) if self.resource_meta else None
            self.journal.prepare(op, resource)
            # Narrow external-editor race. This is not a filesystem CAS against hostile writers.
            if self._snapshot(path) != (before, before_mode):
                raise ValueError('版本冲突：提交前文件发生变化')
            target = self.root/self.path(path)
            if data is None:
                target.unlink()
                self._sync_dir(target.parent)
            else:
                self._atomic(target, data, after_mode)
            self._finish(op)
            return {'change_id':op['id'], 'path':path, 'version':op['after_hash']}

    def write(self, path, content, expected=_UNSET, reverts=None):
        data = content.encode('utf-8')
        if len(data) > 1_000_000:
            raise ValueError('文件超过大小限制')
        return self._mutate(path, data, expected, reverts)

    def read_binary_version(self, path):
        data = self.read_bytes(path)
        return data, digest(data)

    def write_bytes(self, path, content, *, expected, operation_id=None):
        if not isinstance(content, bytes) or len(content) > 32_000_000:
            raise ValueError('二进制文件上限 32MB')
        return self._mutate(path, content, expected=expected, operation_id=operation_id)

    def delete_version(self, path, *, expected, operation_id=None):
        if expected is None:raise ValueError('删除需要已有文件版本')
        return self._mutate(path, None, expected=expected, operation_id=operation_id, kind='file.deleted')

    def delete(self, path):
        data = self.read_bytes(path)
        return self._mutate(path, None, expected=digest(data), kind='file.deleted')['change_id']

    def revert(self, cid):
        old = self.journal.committed(cid, self.session, str(self.root))
        if not old:
            raise ValueError('变更不存在或属于旧 Blob 工作空间；旧版本请单独导出')
        return self._mutate(old['path'], self._blob(old['before_hash']), expected=old['after_hash'],
                            reverts=cid, mode=old['before_mode'], kind='file.reverted')['change_id']
