"""File operations shared by tool adapters; no model-framework dependency."""
import json

from artorias_shared.ports.execution import WorkspacePort

PAGE_BYTES = 8192


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False).encode('utf-8')


def _page_bounds(offset, limit, maximum, total, version, expected_version):
    if offset < 0 or not 1 <= limit <= maximum:
        raise ValueError('分页范围无效')
    if offset and not expected_version:
        raise ValueError('读取下一页时请传入上一页的 version 作为 expected_version')
    if expected_version is not None and version != expected_version:
        raise ValueError('内容已变更，请从 offset=0 重新读取')
    if offset > total:
        raise ValueError('分页范围无效')


def _byte_capped_page(values, offset, limit):
    """Find the largest prefix that fits the JSON content byte cap."""
    start, stop = offset, min(len(values), offset + limit)
    while start < stop:
        end = (start + stop + 1) // 2
        if len(json_bytes(values[offset:end])) <= PAGE_BYTES:
            start = end
        else:
            stop = end - 1
    if start == offset and offset < len(values):
        raise ValueError('单项超过分页字节上限')
    return values[offset:start], start if start < len(values) else None

class FileToolService:
    def __init__(self, workspace: WorkspacePort):
        self.workspace = workspace
        # Execution-local baseline. After recreation callers must read again.
        self.observed_versions = {}

    def read(self, path):
        text, version = self.workspace.read_version(path)
        self.observed_versions[self.workspace.path(path)] = version
        return text

    def read_page(self, path, offset, limit, expected_version):
        text, version = self.workspace.read_version(path)
        _page_bounds(offset, limit, 4000, len(text), version, expected_version)
        content, next_offset = _byte_capped_page(text, offset, limit)
        self.observed_versions[self.workspace.path(path)] = version
        return {
            'path': path, 'content': content, 'version': version, 'offset': offset,
            'total_chars': len(text), 'next_offset': next_offset, 'has_more': next_offset is not None,
        }

    def write(self, path, content):
        key = self.workspace.path(path)
        if key not in self.observed_versions and self.workspace.head(path) is not None:
            raise ValueError('覆盖已有文件前必须先 project_read')
        result = self.workspace.write(path, content, expected=self.observed_versions.get(key))
        self.observed_versions[key] = result['version']
        return result

    def edit(self, path, old, new, expected_version=None):
        text, version = self.workspace.read_version(path)
        if expected_version is not None and version != expected_version:
            return {'status': 'conflict', 'reason': 'version_mismatch', 'path': path}
        count = text.count(old) if old else 0
        if count != 1:
            reason = 'ambiguous_match' if count > 1 else 'no_match' if old else 'empty_match'
            return {'status': 'conflict', 'reason': reason, 'matches': count, 'path': path}
        result = self.workspace.write(path, text.replace(old, new, 1), expected=version)
        self.observed_versions[self.workspace.path(path)] = result['version']
        return result
