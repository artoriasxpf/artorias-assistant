"""Bounded, read-only filesystem operations at the device boundary."""

import os
import stat
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from time import monotonic

from .local_workspace import digest

MAX_TEXT_BYTES = 1_000_000
MAX_SCAN_ENTRIES = 50_000
IGNORED_DIRECTORIES = frozenset({
    '.git', '.venv', 'node_modules', '__pycache__', 'dist', 'build', 'vendor',
})


@contextmanager
def _open_relative(root, relative, *, directory):
    """Open through directory descriptors so a swapped symlink cannot escape the root."""
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    relative = Path(relative)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('需要工作空间内的相对路径')
    if not directory and not relative.parts:
        raise ValueError('需要文件路径')
    fd = os.open(root.anchor, directory_flags)
    parts = (*root.parts[1:], *relative.parts)
    try:
        for index, part in enumerate(parts):
            flags = directory_flags
            if not directory and index == len(parts) - 1:
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def entries(workspace, relative, *, max_depth, include_hidden):
    """Yield metadata in stable directory/name order with bounded per-directory work."""
    workspace._directory(relative)
    pending = deque([('.', 0)])
    inspected = 0
    while pending:
        directory, depth = pending.popleft()
        found = []
        stop_reason = None
        deadline = monotonic() + 2
        try:
            with _open_relative(workspace.root, Path(relative) / directory, directory=True) as fd:
                with os.scandir(fd) as children:
                    for child in children:
                        if inspected >= MAX_SCAN_ENTRIES or monotonic() >= deadline:
                            stop_reason = 'scan_limit' if inspected >= MAX_SCAN_ENTRIES else 'time_limit'
                            break
                        inspected += 1
                        path = (Path(directory) / child.name).as_posix()
                        try:
                            path.encode('utf-8')
                            if child.name.startswith('.') and not include_hidden:
                                record = {'skip': 'hidden'}
                            elif child.is_symlink():
                                record = {'skip': 'symlink'}
                            elif child.is_dir(follow_symlinks=False):
                                if child.name in IGNORED_DIRECTORIES:
                                    record = {'skip': 'ignored_directory'}
                                else:
                                    record = {'path': path, 'type': 'directory'}
                            elif child.is_file(follow_symlinks=False):
                                info = child.stat(follow_symlinks=False)
                                record = {'path': path, 'type': 'file', 'size_bytes': info.st_size}
                            else:
                                record = {'skip': 'special_file'}
                        except UnicodeError:
                            record = {'skip': 'filename_encoding'}
                        except OSError:
                            record = {'skip': 'metadata_error', 'error': True}
                        found.append((child.name, record))
        except OSError:
            yield {'skip': 'directory_error', 'error': True}
            continue
        for _, record in sorted(found):
            if record.get('type') == 'directory':
                if depth + 1 < max_depth:
                    pending.append((record['path'], depth + 1))
                else:
                    record = dict(record, depth_limited=True)
            yield record
        if stop_reason:
            yield {'stop_reason': stop_reason}
            return


def read_text(workspace, relative, *, max_bytes=MAX_TEXT_BYTES, versioned=True):
    """Read a small regular text file and return the write-compatible version on demand."""
    with _open_relative(workspace.root, workspace.path(relative), directory=False) as fd:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            return {'status': 'special_file', 'read_bytes': 0}
        if before.st_size > max_bytes:
            return {'status': 'file_too_large', 'read_bytes': 0}
        with os.fdopen(os.dup(fd), 'rb') as stream:
            data = stream.read(max_bytes + 1)
        after = os.fstat(fd)
    result = {'read_bytes': len(data)}
    if len(data) > max_bytes:
        return dict(result, status='file_too_large')
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_size, after.st_mtime_ns, after.st_ctime_ns
    ):
        return dict(result, status='stale')
    if b'\0' in data:
        return dict(result, status='binary')
    try:
        content = data.decode('utf-8')
    except UnicodeDecodeError:
        return dict(result, status='encoding')
    observation = f'{after.st_dev}:{after.st_ino}:{after.st_size}:{after.st_mtime_ns}:{after.st_ctime_ns}'
    return dict(result, status='ok', content=content,
                version=digest(data) if versioned else None, observation=observation)


def search_text(workspace, relative, *, query, mode, context_lines, max_hits,
                start_line, observation, timeout_seconds, max_bytes):
    import regex

    deadline = monotonic() + timeout_seconds
    result = read_text(workspace, relative, max_bytes=max_bytes, versioned=False)
    content = result.pop('content', None)
    if content is None:
        return result
    if observation is not None and observation != result['observation']:
        return dict(result, status='stale')
    pattern = regex.compile(query) if mode == 'regex' else None
    lines = content.splitlines()
    hits = []
    for index in range(start_line, len(lines)):
        remaining = deadline - monotonic()
        if remaining <= 0:
            return dict(result, hits=hits, next_line=index)
        line = lines[index]
        try:
            match = pattern.search(line, timeout=min(remaining, .05)) if pattern else None
        except TimeoutError:
            return dict(result, status='regex_timeout', hits=hits)
        column = match.start() if match else line.find(query) if pattern is None else -1
        if column < 0:
            continue
        snippet = line[max(0, column - 40):column + 160]
        before = [item[-100:] for item in lines[max(0, index - context_lines):index]]
        after = [item[:100] for item in lines[index + 1:index + 1 + context_lines]]
        hits.append({'line': index + 1, 'column': column + 1, 'snippet': snippet,
                     'before': before, 'after': after, 'observation': result['observation']})
        if len(hits) == max_hits and index + 1 < len(lines):
            return dict(result, hits=hits, next_line=index + 1)
    return dict(result, hits=hits)
