"""Client-side file moves, including journal recovery across volumes."""
from contextlib import nullcontext
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile

from artorias_shared.ids import uid


def _rename_exclusive(source, destination):
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin':
        rename = libc.renamex_np
        args = (os.fsencode(source), os.fsencode(destination), 0x4)  # RENAME_EXCL
    elif sys.platform.startswith('linux') and hasattr(libc, 'renameat2'):
        rename = libc.renameat2
        args = (-100, os.fsencode(source), -100, os.fsencode(destination), 1)  # AT_FDCWD, RENAME_NOREPLACE
    else:
        raise OSError(errno.ENOTSUP, '文件系统不支持原子不覆盖移动')
    if rename(*args):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(destination))


def _identity(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode]


def _version(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _exact_entry_exists(path):
    with os.scandir(path.parent) as children:
        return any(child.name == path.name for child in children)


def operation_paths(op):
    if not op.get('move_data'):
        return (Path(op['root']) / op['path'],)
    move = json.loads(op['move_data'])
    return tuple(Path(move[key]['path']) for key in ('source', 'destination'))


def recover_move(workspace, op):
    """Confirm disk evidence; never delete a surviving source during recovery."""
    move = json.loads(op['move_data'])
    source, destination = operation_paths(op)
    paths = (source, destination)
    for path, key in zip(paths, ('source', 'destination')):
        root = Path(move[key]['root'])
        if root.resolve() != root or path.resolve() != path:
            return
    if (not source.parent.is_dir() or not destination.parent.is_dir()
            or source.parent.stat().st_dev != move['source_parent_device']
            or destination.parent.stat().st_dev != move['destination_parent_device']):
        return
    before, after = _identity(source), _identity(destination)
    if move['case_only']:
        before = before if _exact_entry_exists(source) else None
        after = after if _exact_entry_exists(destination) else None
    if before is None and after == move['destination_identity'] and _version(destination) == move['version']:
        workspace._sync_dir(destination.parent)
        if source.parent != destination.parent:
            workspace._sync_dir(source.parent)
        workspace.journal.finish(op)
    elif before == move['source_identity'] and after is None:
        if move.get('stage'):
            stage = Path(move['stage'])
            if stage.parent == destination.parent and _identity(stage) == move['destination_identity']:
                stage.unlink()
        workspace.journal.abort(op['id'])


def move_file(workspace, source, other, destination, *, expected_version=None, reverts=None):
    if (workspace.blobs != other.blobs or workspace.journal is not other.journal
            or (workspace.session, workspace.run) != (other.session, other.run)):
        raise ValueError('移动的两端必须由同一客户端和运行处理')
    other_gate = other.execution_access() if other.execution_gate != workspace.execution_gate else nullcontext()
    with workspace._locked(), other_gate:
        source_path = workspace.root / workspace.path(source)
        destination_path = other.root / other.path(destination)
        if source_path == destination_path:
            raise ValueError('源路径和目标路径相同')
        workspace._recover()
        if other.root != workspace.root:
            other._recover()
        touched = {source_path, destination_path}
        if any(touched.intersection(operation_paths(op)) for op in workspace.journal.pending()):
            raise ValueError('存在未解决的中断操作；请先核对源文件和目标文件')
        original = _identity(source_path)
        if original is None:
            raise FileNotFoundError(source)
        if not stat.S_ISREG(original[-1]):
            raise ValueError('project_move 只支持普通文件')
        destination_identity = _identity(destination_path)
        case_only = (destination_identity == original and source_path.parent == destination_path.parent
                     and source_path.name.casefold() == destination_path.name.casefold()
                     and _exact_entry_exists(source_path) and not _exact_entry_exists(destination_path))
        if destination_identity is not None and not case_only:
            raise FileExistsError(f'目标已存在：{destination}')
        source_disk = source_path.parent.stat().st_dev
        destination_disk = destination_path.parent.stat().st_dev
        version = _version(source_path)
        if expected_version is not None and version != expected_version:
            raise ValueError('版本冲突：源文件已修改')

        stage = None
        try:
            if original[0] != destination_disk:
                fd, stage_name = tempfile.mkstemp(prefix='.artorias-move-', dir=destination_path.parent)
                stage = Path(stage_name)
                with os.fdopen(fd, 'wb') as output, source_path.open('rb') as input_file:
                    shutil.copyfileobj(input_file, output, length=1024 * 1024)
                    output.flush()
                    shutil.copystat(source_path, stage)
                    os.fsync(output.fileno())
                if _version(stage) != version:
                    raise ValueError('版本冲突：复制时源文件发生变化')
                workspace._sync_dir(destination_path.parent)

            if _identity(source_path) != original:
                raise ValueError('版本冲突：源文件已修改')
            details = {
                'source': dict(path=str(source_path), root=str(workspace.root),
                               workspace_id=(workspace.resource_meta or {}).get('workspace_id')),
                'destination': dict(path=str(destination_path), root=str(other.root),
                                    workspace_id=(other.resource_meta or {}).get('workspace_id')),
                'source_identity': original,
                'destination_identity': _identity(stage) if stage else original,
                'source_parent_device': source_disk,
                'destination_parent_device': destination_disk,
                'case_only': case_only,
                'stage': str(stage) if stage else None,
                'version': version,
            }
            op = dict(id=uid(), root=str(workspace.root), session=workspace.session, run=workspace.run,
                      path=workspace.path(source), before_hash=None, after_hash=None, before_mode=None,
                      after_mode=None, reverts=reverts, kind='file.moved', status='pending',
                      move_data=json.dumps(details, ensure_ascii=False))
            resource = dict(workspace.resource_meta, blob_root=str(workspace.blobs)) if workspace.resource_meta else None
            workspace.journal.prepare(op, resource)
            workspace.path(source)
            other.path(destination)
            if _identity(source_path) != original:
                workspace.journal.abort(op['id'])
                raise ValueError('版本冲突：源文件已修改')
            try:
                _rename_exclusive(stage or source_path, destination_path)
            except OSError:
                workspace.journal.abort(op['id'])
                raise
            workspace._sync_dir(destination_path.parent)
            if stage:
                workspace.path(source)
                other.path(destination)
                if (_identity(source_path) != original
                        or _identity(destination_path) != details['destination_identity']):
                    raise ValueError('移动未完成，请核对源文件和目标文件')
                source_path.unlink()
            if source_path.parent != destination_path.parent:
                workspace._sync_dir(source_path.parent)
            workspace.journal.finish(op)
            return {'change_id': op['id'], 'version': version}
        finally:
            if stage is not None:
                stage.unlink(missing_ok=True)
