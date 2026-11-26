"""Model-facing project analysis, with bounded output and scoped continuation."""
from collections import Counter, OrderedDict
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from threading import Lock
from time import monotonic
from typing import Annotated, Literal

import regex
from pydantic import BaseModel, ConfigDict, Field, model_validator

from artorias_shared.ids import uid
from artorias_shared.workspace import ConnectorUnavailable
from .files import json_bytes

OUTPUT_BYTES = 24 * 1024
TEXT_BYTES = 1_000_000
PAGE_ENTRIES = 2_000
FILESIZELIMIT = 8_000_000
PathArgument = Annotated[str, Field(min_length=1, max_length=4096)]
Glob = Annotated[str, Field(min_length=1, max_length=256)]


class Arguments(BaseModel):
    model_config = ConfigDict(extra='forbid')


class InventoryArguments(Arguments):
    root: PathArgument = '.'
    include: list[Glob] = Field(default_factory=list, max_length=20)
    exclude: list[Glob] = Field(default_factory=list, max_length=20)
    include_hidden: bool = False
    max_depth: int = Field(default=12, ge=1, le=32)
    max_entries: int = Field(default=PAGE_ENTRIES, ge=1, le=PAGE_ENTRIES)
    cursor: str | None = Field(default=None, max_length=80)


class SearchArguments(Arguments):
    root: PathArgument | None = None
    inventory_id: str | None = Field(default=None, max_length=80)
    query: str = Field(min_length=1, max_length=256)
    mode: Literal['name', 'literal', 'regex'] = 'literal'
    include: list[Glob] = Field(default_factory=list, max_length=20)
    context_lines: int = Field(default=2, ge=0, le=2)
    max_hits: int = Field(default=50, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=80)

    @model_validator(mode='after')
    def validate_search(self):
        if self.root is not None and self.inventory_id is not None:
            raise ValueError('root 和 inventory_id 只能指定一个')
        if '\n' in self.query or '\r' in self.query:
            raise ValueError('搜索表达式必须在单行内')
        if self.mode == 'regex':
            try:
                regex.compile(self.query)
            except regex.error as error:
                raise ValueError(f'无效正则表达式: {error}') from error
        return self


class ReadItem(Arguments):
    path: PathArgument
    expected_version: str | None = Field(default=None, max_length=256)
    line_start: int | None = Field(default=None, ge=1)
    line_count: int | None = Field(default=None, ge=1, le=400)
    offset: int | None = Field(default=None, ge=0)
    limit: int | None = Field(default=None, ge=1, le=8000)

    @model_validator(mode='after')
    def validate_range(self):
        if (self.line_start is not None or self.line_count is not None) and (
            self.offset is not None or self.limit is not None
        ):
            raise ValueError('行范围与字符范围只能指定一种')
        return self


class ReadArguments(Arguments):
    items: list[ReadItem] = Field(min_length=1, max_length=8)
    max_output_bytes: int = Field(default=OUTPUT_BYTES, ge=1024, le=OUTPUT_BYTES)


class CursorCache:
    """Single-use cursors. Only inventory parameters and scan metadata are retained."""

    def __init__(self):
        self.entries = OrderedDict()
        self.lock = Lock()

    def put(self, scope, kind, value):
        token = uid()
        with self.lock:
            self.entries[token] = (monotonic(), scope, kind, value)
            if len(self.entries) > 64:
                self.entries.popitem(last=False)
        return token

    def get(self, token, scope, kind, *, consume=False):
        with self.lock:
            entry = self.entries.get(token)
            if not entry or entry[1:3] != (scope, kind):
                return None
            if monotonic() - entry[0] >= 900:
                del self.entries[token]
                return None
            if consume:
                del self.entries[token]
            else:
                self.entries[token] = (monotonic(), *entry[1:])
                self.entries.move_to_end(token)
            return entry[3]


CACHED_SCANS = CursorCache()


def coverage():
    return {'enumerated': 0, 'content_files': 0, 'metadata_only': 0, 'read_bytes': 0,
            'errors': 0, 'skipped': {}, 'total_entries': None}


def skip(stats, reason, *, error=False):
    stats['skipped'][reason] = stats['skipped'].get(reason, 0) + 1
    stats['errors'] += int(error)


def stopped(reason):
    return {'coverage': coverage(), 'truncated': True, 'stop_reason': reason, 'next_cursor': None}


def matches(path, patterns):
    return any(fnmatchcase(path, pattern) or fnmatchcase(path.rsplit('/', 1)[-1], pattern)
               or (pattern.startswith('**/') and fnmatchcase(path, pattern[3:])) for pattern in patterns)


def resource_path(root, child):
    if root.startswith('workspace://'):
        return f"{root.rstrip('/')}/{child}"
    return str(PurePosixPath(root) / child)


@dataclass
class Scan:
    key: tuple
    entries: Iterator[dict]
    pending: tuple | None = None


class ProjectAnalysis:
    def __init__(self, workspace, files):
        self.workspace = workspace
        self.files = files
        self.scope = tuple(getattr(workspace, field, None) for field in ('session', 'run', 'task', 'step'))

    def inventory_options(self, token):
        return CACHED_SCANS.get(token, self.scope, 'inventory_id')

    def _open(self, kind, root, depth, hidden, cursor, options):
        if not hasattr(self.workspace, 'analysis_root'):
            return None, 'unsupported'
        endpoint, relative, identity = self.workspace.analysis_root(root)
        entries = getattr(endpoint, 'analysis_entries', None)
        if entries is None:
            return None, 'unsupported'
        key = (identity, root, depth, hidden, json_bytes(options))
        if cursor:
            existing = CACHED_SCANS.get(cursor, (self.scope, key), kind, consume=True)
            return (existing, None) if existing else (None, 'cursor_expired')
        return Scan(key, iter(entries(relative, max_depth=depth, include_hidden=hidden))), None

    def _page(self, scan, stats, limit, deadline):
        for _ in range(limit):
            if monotonic() >= deadline:
                return 'time_limit'
            item = next(scan.entries, None)
            if item is None:
                return 'complete'
            if 'stop_reason' in item:
                return item['stop_reason'] + '_incomplete'
            stats['enumerated'] += 1
            if item.get('skip'):
                skip(stats, item['skip'], error=item.get('error', False))
                continue
            if item.get('depth_limited'):
                skip(stats, 'depth_limit')
            yield item
        return 'entry_limit'

    def _result(self, kind, scan, stats, reason, **payload):
        continuable = reason in {'entry_limit', 'time_limit', 'hit_limit', 'output_limit', 'read_limit'}
        return dict(payload, coverage=stats, truncated=reason != 'complete' or bool(stats['skipped']),
                    stop_reason=reason, next_cursor=CACHED_SCANS.put(
                        (self.scope, scan.key), kind, scan) if continuable else None)

    def inventory(self, args):
        stats = coverage()
        opts = args.model_dump(exclude={'cursor'})
        scan, error = self._open('inventory', args.root, args.max_depth, args.include_hidden, args.cursor, opts)
        if error:
            return stopped(error)
        inventory_id = CACHED_SCANS.put(self.scope, 'inventory_id', opts)
        extensions, directories, sizes, types = Counter(), Counter(), Counter(), Counter()
        samples, largest = [], []
        page = self._page(scan, stats, args.max_entries, monotonic() + 2)
        while True:
            try:
                item = next(page)
            except StopIteration as end:
                reason = end.value
                break
            path = item['path']
            if matches(path, args.exclude) or (args.include and not matches(path, args.include)):
                skip(stats, 'filtered')
                continue
            stats['metadata_only'] += 1
            types[item['type']] += 1
            directories[path.split('/')[0] if '/' in path or item['type'] == 'directory' else '.'] += 1
            if len(samples) < 8 and len(path.encode()) <= 512:
                samples.append(item)
            if item['type'] == 'file':
                extension = PurePosixPath(path).suffix.lower() or '(none)'
                extensions[extension[:100]] += 1
                size = item['size_bytes']
                sizes['under_1MiB' if size < 2**20 else '1_to_100MiB' if size < 100 * 2**20 else '100MiB_plus'] += 1
                if len(path.encode()) <= 512:
                    largest = sorted([*largest, item], key=lambda row: (-row['size_bytes'], row['path']))[:8]
        result = self._result(
            'inventory', scan, stats, reason, inventory_id=inventory_id, root=args.root,
            counts=dict(types), extensions=dict(extensions.most_common(20)),
            top_directories=dict((key[:100], count) for key, count in directories.most_common(12)),
            size_buckets=dict(sizes), samples=samples, largest=largest, counts_scope='page',
        )
        while len(json_bytes(result)) > OUTPUT_BYTES and (samples or largest):
            (samples if samples else largest).pop()
        return result

    def search(self, args, inventory):
        root = args.root or (inventory['root'] if inventory else '.')
        depth = inventory['max_depth'] if inventory else 12
        hidden = inventory['include_hidden'] if inventory else False
        options = (args.model_dump(exclude={'cursor'}), inventory)
        stats = coverage()
        scan, error = self._open('search', root, depth, hidden, args.cursor, options)
        if error:
            return stopped(error)
        deadline = monotonic() + 2
        page = self._page(scan, stats, PAGE_ENTRIES, deadline)
        hits = []
        size = 0
        while True:
            if scan.pending:
                item, start_line, observation = scan.pending
                scan.pending = None
            else:
                try:
                    item = next(page)
                except StopIteration as end:
                    reason = end.value
                    break
                start_line, observation = 0, None
            path = item['path']
            if ((inventory and (matches(path, inventory['exclude']) or
                                (inventory['include'] and not matches(path, inventory['include']))))
                    or (args.include and not matches(path, args.include))):
                skip(stats, 'filtered')
                continue
            if len(path.encode()) > 1024:
                skip(stats, 'path_limit')
                continue
            target = resource_path(root, path)
            if args.mode == 'name':
                stats['metadata_only'] += 1
                found = [{'path': target, 'line': None, 'snippet': path}] if args.query.casefold() in PurePosixPath(path).name.casefold() else []
            elif item['type'] != 'file':
                stats['metadata_only'] += 1
                continue
            else:
                remaining = deadline - monotonic()
                bytes_left = FILESIZELIMIT - stats['read_bytes']
                if remaining <= 0 or bytes_left < TEXT_BYTES:
                    scan.pending = (item, start_line, observation)
                    reason = 'time_limit' if remaining <= 0 else 'read_limit'
                    break
                try:
                    response = self.workspace.analysis_search(
                        target, query=args.query, mode=args.mode, context_lines=args.context_lines,
                        max_hits=args.max_hits - len(hits), start_line=start_line,
                        observation=observation, timeout_seconds=remaining, max_bytes=TEXT_BYTES,
                    )
                except ConnectorUnavailable:
                    return stopped('unsupported')
                except (OSError, ValueError, KeyError):
                    skip(stats, 'unreadable', error=True)
                    continue
                stats['read_bytes'] += response['read_bytes']
                status = response['status']
                if status != 'ok':
                    skip(stats, status, error=status in {'stale', 'regex_timeout'})
                found = [dict(hit, path=target) for hit in response.get('hits', [])]
            for hit in found:
                cost = len(json_bytes(hit)) + 1
                envelope = len(json_bytes({'root': root, 'coverage': stats}))
                if size + cost + envelope > OUTPUT_BYTES - 512:
                    if not hits:
                        skip(stats, 'output_limit')
                        continue
                    scan.pending = (item, hit['line'] - 1 if hit['line'] else 0, hit.get('observation'))
                    return self._result('search', scan, stats, 'output_limit', hits=hits, root=root)
                hits.append(hit)
                size += cost
            if args.mode != 'name':
                if 'next_line' in response:
                    scan.pending = (item, response['next_line'], response['observation'])
                elif response['status'] == 'ok':
                    stats['content_files'] += 1
            if len(hits) >= args.max_hits:
                reason = 'hit_limit'
                break
            if monotonic() >= deadline:
                reason = 'time_limit'
                break
        return self._result('search', scan, stats, reason, hits=hits, root=root)

    @staticmethod
    def _metadata_limit(result):
        """Report item positions when their original paths exceed the output budget."""
        return dict(
            result, truncated=True, stop_reason='output_limit',
            items=[{'index': index, 'status': row['status'] if row['status'] != 'ok' else 'output_limit'}
                   for index, row in enumerate(result['items'])],
        )

    def read_many(self, args, grants):
        stats = coverage()
        result = dict(items=[], coverage=stats, truncated=False, stop_reason='complete', next_cursor=None)

        def read(path):
            allowed, grant = grants[path]
            if not allowed:
                return {'status': 'permission_denied'}, None
            if not hasattr(self.workspace, 'analysis_text'):
                return {'status': 'unsupported'}, None
            token = self.workspace.once.set(grant) if grant else None
            try:
                return self.workspace.analysis_text(path, max_bytes=TEXT_BYTES, versioned=True)
            except ConnectorUnavailable:
                return {'status': 'unsupported'}, None
            except PermissionError:
                return {'status': 'permission_denied'}, None
            except FileNotFoundError:
                return {'status': 'not_found'}, None
            except (OSError, ValueError, KeyError):
                return {'status': 'unreadable'}, None
            finally:
                if token is not None:
                    self.workspace.once.reset(token)

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {path: pool.submit(copy_context().run, read, path) for path in grants}
            # One physical read per requested path, including repeated ranges.
            loaded = {path: future.result() for path, future in futures.items()}
        for value, _ in loaded.values():
            stats['read_bytes'] += value.get('read_bytes', 0)
        stats['content_files'] = sum(value['status'] == 'ok' for value, _ in loaded.values())
        pages = []
        for item in args.items:
            value, key = loaded[item.path]
            status = value['status']
            version = value.get('version')
            if status == 'ok' and item.expected_version is not None and version != item.expected_version:
                status = 'stale'
            row = {'path': item.path, 'status': status}
            result['items'].append(row)
            if status != 'ok':
                skip(stats, status, error=True)
                continue
            by_line = item.line_start is not None or item.line_count is not None
            units = value['content'].splitlines(keepends=True) if by_line else value['content']
            first = (item.line_start or 1) - 1 if by_line else (item.offset or 0)
            count = (item.line_count or 80) if by_line else (item.limit or 2000)
            if first > len(units):
                row['status'] = 'invalid_range'
                skip(stats, 'invalid_range', error=True)
                continue
            unit = 'line' if by_line else 'unicode_character'
            row.update(unit=unit, start=first + int(by_line), count=0, next_start=None,
                       total=len(units), version=version, content='')
            pages.append((row, units, first, count, by_line, key))
        stats['enumerated'] = len(args.items)
        if len(json_bytes(result)) + len(pages) * 40 > args.max_output_bytes:
            return self._metadata_limit(result)
        for index, (row, units, first, count, by_line, key) in enumerate(pages):
            available = args.max_output_bytes - len(json_bytes(result)) - len(pages) * 40
            remaining = len(pages) - index
            allowance = max(0, available // remaining)
            low, high = 0, min(count, len(units) - first)
            while low < high:
                middle = (low + high + 1) // 2
                content = ''.join(units[first:first + middle])
                if len(json_bytes(content)) - 2 <= allowance:
                    low = middle
                else:
                    high = middle - 1
            end = first + low
            row.update(content=''.join(units[first:end]), count=low,
                       next_start=end + int(by_line) if end < len(units) else None)
            if not low and first < len(units):
                row['status'] = 'output_limit'
                skip(stats, 'output_limit')
            elif version := row['version']:
                self.files.observed_versions[key] = version
        result['truncated'] = bool(stats['skipped']) or any(row.get('next_start') is not None for row in result['items'])
        if result['truncated']:
            result['stop_reason'] = 'partial'
        if len(json_bytes(result)) > args.max_output_bytes:
            return self._metadata_limit(result)
        return result
