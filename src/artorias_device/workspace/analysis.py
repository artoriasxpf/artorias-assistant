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


def observation(info):
    return f'{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}'

def file_observation(workspace, relative):
    with _open_relative(workspace.root, workspace.path(relative), directory=False) as fd:
        return observation(os.fstat(fd))


def read_range(workspace, relative, *, offset, limit, expected_version=None):
    """Read bounded UTF-8 byte ranges, binding continuations to the observation."""
    import codecs

    with _open_relative(workspace.root, workspace.path(relative), directory=False) as fd:
        info = os.fstat(fd)
        version = observation(info)
        if not stat.S_ISREG(info.st_mode):
            return {'status': 'unsupported'}
        if expected_version and expected_version != version:
            return {'status': 'stale'}
        if not 0 <= offset <= info.st_size or not 4 <= limit <= 4096:
            return {'status': 'invalid_range'}
        data = os.pread(fd, limit, offset)
        if observation(os.fstat(fd)) != version:
            return {'status': 'stale'}
    if b'\0' in data:
        return {'status': 'binary'}
    decoder = codecs.getincrementaldecoder('utf-8')()
    try:
        content = decoder.decode(data, final=offset + len(data) >= info.st_size)
    except UnicodeDecodeError:
        return {'status': 'invalid_utf8_range'}
    remaining, _ = decoder.getstate()
    end = offset + len(data) - len(remaining)
    return {'status': 'ok', 'version': version, 'version_kind': 'observation',
            'unit': 'byte', 'start': offset, 'end': end, 'total': info.st_size,
            'next_start': end if end < info.st_size else None, 'content': content}


def inventory_page(workspace, relative, *, request_id, cursor, limit, include_hidden=False):
    """Store metadata pages in the endpoint's journal area for retry by request ID."""
    import json
    import sqlite3
    from contextlib import closing
    from uuid import uuid4
    from collections import Counter

    scope = json.dumps([workspace.session, str(workspace.root), relative] + ([True] if include_hidden else []))
    db_path = workspace.blobs / 'analysis-pages.sqlite'
    with closing(sqlite3.connect(db_path, timeout=10)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS inventory_pages(
            request_id TEXT PRIMARY KEY, cursor TEXT UNIQUE, scope TEXT NOT NULL,
            parent TEXT, page_size INTEGER NOT NULL, position INTEGER NOT NULL,
            snapshot_id TEXT NOT NULL, payload TEXT NOT NULL,
            timestamp REAL NOT NULL DEFAULT (unixepoch()))""")
        db.execute("""CREATE TABLE IF NOT EXISTS inventory_entries(
            snapshot_id TEXT NOT NULL, ordinal INTEGER NOT NULL, entry TEXT NOT NULL,
            PRIMARY KEY(snapshot_id,ordinal))""")
        db.execute('BEGIN IMMEDIATE')
        db.execute('DELETE FROM inventory_pages WHERE timestamp < unixepoch() - 86400')
        db.execute("""DELETE FROM inventory_entries WHERE snapshot_id NOT IN (
            SELECT snapshot_id FROM inventory_pages)""")
        old = db.execute(
            'SELECT scope,parent,page_size,payload FROM inventory_pages WHERE request_id=?',
            (request_id,),
        ).fetchone()
        if old:
            if old[:3] != (scope, cursor, limit):
                return {'status': 'conflict'}
            return json.loads(old[3])
        position = 0
        snapshot_id = request_id
        if cursor:
            prior = db.execute('SELECT position,snapshot_id FROM inventory_pages WHERE cursor=? AND scope=?',
                               (cursor, scope)).fetchone()
            if not prior:
                return {'status': 'cursor_expired'}
            position, snapshot_id = prior
        else:
            count = db.execute('SELECT count(*) FROM inventory_entries').fetchone()[0]
            if count >= 250_000:
                return {'status': 'inventory_quota'}
            db.executemany('INSERT INTO inventory_entries VALUES(?,?,?)', (
                (snapshot_id, n, json.dumps(item, ensure_ascii=False))
                for n, item in enumerate(entries(workspace, relative, max_depth=32, include_hidden=include_hidden))
            ))
        page = [json.loads(row[0]) for row in db.execute(
            """SELECT entry FROM inventory_entries WHERE snapshot_id=? AND ordinal>=?
                ORDER BY ordinal LIMIT ?""", (snapshot_id, position, limit + 1)
        )]
        next_cursor = uuid4().hex if len(page) > limit else None
        page = page[:limit]
        skipped = Counter(entry['skip'] for entry in page if entry.get('skip'))
        result = {'status': 'ok', 'entries': page, 'next_cursor': next_cursor,
                  'coverage': {'skipped': dict(skipped)},
                  'stop_reason': next((entry['stop_reason'] for entry in page if 'stop_reason' in entry),
                                      'page_limit' if next_cursor else 'complete'),
                  'snapshot_id': snapshot_id,
                  'incomplete': any('stop_reason' in entry or entry.get('error') or
                                    entry.get('depth_limited') or entry.get('skip')
                                    for entry in page)}
        db.execute("""INSERT INTO inventory_pages(
            request_id,cursor,scope,parent,page_size,position,snapshot_id,payload)
            VALUES(?,?,?,?,?,?,?,?)""",
            (request_id, next_cursor, scope, cursor, limit, position + len(page),
             snapshot_id, json.dumps(result, ensure_ascii=False)))
        return result


def search_page(workspace, relative, *, request_id, cursor, query, include_hidden=False):
    """Search one persisted inventory page, resuming within a file when needed."""
    import json
    import sqlite3
    from contextlib import closing
    from uuid import uuid4

    scope = json.dumps([workspace.session, str(workspace.root), relative, query] + ([True] if include_hidden else []))
    with closing(sqlite3.connect(workspace.blobs / 'analysis-search.sqlite', timeout=10)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS search_pages(
            request_id TEXT PRIMARY KEY, cursor TEXT UNIQUE, scope TEXT NOT NULL,
            parent TEXT, payload TEXT NOT NULL, continuation TEXT NOT NULL,
            timestamp REAL NOT NULL DEFAULT (unixepoch()))""")
        db.execute('BEGIN IMMEDIATE')
        db.execute('DELETE FROM search_pages WHERE timestamp < unixepoch() - 86400')
        old = db.execute('SELECT scope,parent,payload FROM search_pages WHERE request_id=?',
                         (request_id,)).fetchone()
        if old:
            return json.loads(old[2]) if old[:2] == (scope, cursor) else {'status': 'conflict'}
        if cursor:
            row = db.execute('SELECT continuation FROM search_pages WHERE cursor=? AND scope=?',
                             (cursor, scope)).fetchone()
            if not row:
                return {'status': 'cursor_expired'}
            state = json.loads(row[0])
        else:
            state = {'page_id': request_id, 'page_cursor': None, 'position': 0,
                     'line': 0, 'observation': None}
        page = inventory_page(workspace, relative, request_id='search:' + state['page_id'],
                              cursor=state['page_cursor'], limit=8, include_hidden=include_hidden)
        if page['status'] != 'ok':
            return page
        hits, gaps = [], []
        deadline = monotonic() + 2
        while state['position'] < len(page['entries']) and len(hits) < 8 and monotonic() < deadline:
            item = page['entries'][state['position']]
            if item.get('type') == 'file':
                try:
                    result = search_text(
                        workspace, str(Path(relative) / item['path']), query=query, mode='literal',
                        context_lines=0, max_hits=8 - len(hits), start_line=state['line'],
                        observation=state['observation'], timeout_seconds=max(0, deadline - monotonic()),
                        max_bytes=MAX_TEXT_BYTES,
                    )
                except (OSError, ValueError):
                    result = {'status': 'unreadable'}
                hits.extend(dict(hit, path=item['path']) for hit in result.get('hits', []))
                if 'next_line' in result:
                    state.update(line=result['next_line'], observation=result['observation'])
                    break
                if result['status'] != 'ok':
                    gaps.append({'path': item['path'], 'status': result['status']})
            state.update(position=state['position'] + 1, line=0, observation=None)
        more = state['position'] < len(page['entries']) or bool(page['next_cursor'])
        if state['position'] >= len(page['entries']) and page['next_cursor']:
            state.update(page_id=request_id, page_cursor=page['next_cursor'], position=0)
        next_cursor = uuid4().hex if more else None
        payload = {'status': 'ok', 'hits': hits, 'gaps': gaps, 'next_cursor': next_cursor,
                   'incomplete': page['incomplete'] or bool(gaps), 'coverage': page.get('coverage', {}),
                   'stop_reason': 'page_limit' if more else page.get('stop_reason', 'complete')}
        db.execute('INSERT INTO search_pages(request_id,cursor,scope,parent,payload,continuation) VALUES(?,?,?,?,?,?)',
                   (request_id, next_cursor, scope, cursor, json.dumps(payload, ensure_ascii=False),
                    json.dumps(state)))
        return payload
