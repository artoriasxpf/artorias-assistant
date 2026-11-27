"""Preview concrete, versioned workspace edits and apply them through the journal."""
import hashlib
import json
from difflib import unified_diff
from typing import Annotated, Literal

import regex
from pydantic import BaseModel, ConfigDict, Field

from artorias_shared.ids import uid
from artorias_shared.workspace import ConnectorUnavailable

MAX_FILE_BYTES = 1_000_000
PathArgument = Annotated[str, Field(min_length=1, max_length=4096)]
Version = Annotated[str, Field(min_length=1, max_length=256)]


class Arguments(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ReplaceFile(Arguments):
    path: PathArgument
    expected_version: Version | None = None
    expected_matches: int = Field(ge=1, le=1000)


class TextEdit(Arguments):
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    text: str = Field(max_length=100_000)


class PatchFile(Arguments):
    path: PathArgument
    expected_version: Version
    edits: list[TextEdit] = Field(min_length=1, max_length=100)


class ReplacePlan(Arguments):
    kind: Literal['replace']
    files: list[ReplaceFile] = Field(min_length=1, max_length=8)
    mode: Literal['literal', 'regex'] = 'literal'
    pattern: str = Field(min_length=1, max_length=500)
    replacement: str = Field(max_length=10_000)


class PatchPlan(Arguments):
    kind: Literal['patch']
    files: list[PatchFile] = Field(min_length=1, max_length=8)


class PlanArguments(Arguments):
    request: Annotated[ReplacePlan | PatchPlan, Field(discriminator='kind')]


def replace_text(text, request, item):
    pattern = regex.escape(request.pattern) if request.mode == 'literal' else request.pattern
    expression = regex.compile(pattern)
    pieces = []
    previous = matches = output_bytes = 0
    for match in expression.finditer(text, timeout=0.1):
        matches += 1
        if matches > item.expected_matches:
            return None, matches
        replacement = request.replacement if request.mode == 'literal' else match.expand(request.replacement)
        piece = text[previous:match.start()] + replacement
        output_bytes += len(piece.encode())
        if output_bytes > MAX_FILE_BYTES:
            raise ValueError('替换后文件超过大小限制')
        pieces.append(piece)
        previous = match.end()
    if matches != item.expected_matches:
        return None, matches
    return ''.join(pieces) + text[previous:], matches


def patch_text(text, edits):
    ordered = sorted(edits, key=lambda edit: (edit.start, edit.end))
    pieces = []
    end = 0
    previous_start = None
    for edit in ordered:
        if edit.start < end or edit.start == previous_start or not edit.start <= edit.end <= len(text):
            raise ValueError('补丁范围无效、重叠或顺序不明确')
        pieces.extend((text[end:edit.start], edit.text))
        end = edit.end
        previous_start = edit.start
    return ''.join(pieces) + text[end:]


def preview_diff(path, offset, before, after):
    result = []
    for line in unified_diff(before, after, fromfile=path, tofile=path):
        if line.startswith('@@ '):
            old, new = line.split()[1:3]
            ranges = []
            for side in (old, new):
                number, delimiter, count = side[1:].partition(',')
                ranges.append(f'{side[0]}{int(number) + offset}{delimiter}{count}')
            line = f'@@ {ranges[0]} {ranges[1]} @@\n'
        result.append(line)
        if line.startswith(('+', '-', ' ')) and not line.endswith('\n'):
            result.append('\n\\ No newline at end of file\n')
    return ''.join(result)


class ChangePlans:
    def __init__(self, workspace):
        self.workspace = workspace

    def identity(self, path):
        space, target = self.workspace._target(path)
        return [space['kind'], space['location_id'], space.get('id'), str(target)]

    def preview(self, request):
        ws = self.workspace
        items, results = [], []
        seen = set()
        for item in request.files:
            row = {'path': item.path}
            results.append(row)
            try:
                identity = self.identity(item.path)
                physical = (identity[0], identity[1], identity[3])
                if physical in seen:
                    raise ValueError('多个条目指向同一文件')
                seen.add(physical)
                before, version = ws.read_version(item.path)
                row['version'] = version
                if item.expected_version is not None and item.expected_version != version:
                    row.update(status='conflict', reason='version_mismatch')
                    continue
                if isinstance(request, ReplacePlan):
                    after, matches = replace_text(before, request, item)
                    row['matches'] = matches
                    if after is None:
                        row.update(status='conflict', reason='match_count_mismatch',
                                   matches_is_lower_bound=matches > item.expected_matches)
                        continue
                else:
                    after = patch_text(before, item.edits)
                if len(after.encode()) > MAX_FILE_BYTES:
                    raise ValueError('结果超过文件大小限制')
                if before == after:
                    row['status'] = 'unchanged'
                    continue
                before_lines = before.splitlines(keepends=True)
                after_lines = after.splitlines(keepends=True)
                # Keep short edits in large files fast by comparing only the edited region.
                common_start = 0
                for old, new in zip(before_lines, after_lines):
                    if old != new:
                        break
                    common_start += 1
                common_end = 0
                end_limit = min(len(before_lines), len(after_lines)) - common_start
                for old, new in zip(reversed(before_lines), reversed(after_lines)):
                    if common_end >= end_limit or old != new:
                        break
                    common_end += 1
                start = max(0, common_start - 3)
                tail = max(0, common_end - 3)
                left = before_lines[start:len(before_lines) - tail]
                right = after_lines[start:len(after_lines) - tail]
                if max(len(left), len(right)) > 2000:
                    row.update(status='failed', reason='diff_line_limit')
                    continue
                diff = preview_diff(item.path, start, left, right)
                if len(diff.encode()) > 8_192:
                    row.update(status='failed', reason='diff_output_limit')
                    continue
                row.update(status='ready', diff=diff)
                items.append(dict(path=item.path, resource=identity, version=version, content=after))
            except (OSError, ValueError, IndexError, regex.error, ConnectorUnavailable) as error:
                row.update(status='failed', reason=str(error))
        if any(row['status'] in ('failed', 'conflict') for row in results):
            return {'status': 'blocked', 'files': results}
        if sum(len(row.get('diff', '').encode()) for row in results) > 24_576:
            return {'status': 'blocked', 'reason': 'batch_diff_output_limit'}
        if not items:
            return {'status': 'unchanged', 'files': results}
        plan_id = uid()
        with ws.store.tx() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS project_change_plans(
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
                body_json TEXT NOT NULL, expires_at INTEGER NOT NULL)""")
            db.execute('DELETE FROM project_change_plans WHERE expires_at<=unixepoch()')
            db.execute('INSERT INTO project_change_plans (id,session_id,workspace_id,body_json,expires_at) VALUES(?,?,?,?,unixepoch()+86400)',
                       (plan_id, ws.session, ws.workspace['id'], json.dumps(items, ensure_ascii=False)))
        return {'status': 'ready', 'plan_id': plan_id, 'files': results}

    def load(self, plan_id):
        ws = self.workspace
        with ws.store.tx() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='project_change_plans'").fetchone():
                raise ValueError('变更计划不存在或已过期')
            row = db.execute("""SELECT body_json FROM project_change_plans WHERE id=? AND session_id=?
                AND workspace_id=? AND expires_at>unixepoch()""", (plan_id, ws.session, ws.workspace['id'])).fetchone()
        if not row:
            raise ValueError('变更计划不存在或已过期')
        return json.loads(row['body_json'])

    def apply(self, plan_id, items):
        ws = self.workspace
        results, pending = [], []
        for index, item in enumerate(items):
            path = item['path']
            row = {'path': path}
            results.append(row)
            try:
                if self.identity(path) != item['resource']:
                    row.update(status='conflict', reason='resource_changed')
                    continue
                ws.check_access(path, 'write')
                current = ws.head(path)  # Opening the endpoint also reconciles its journal.
                operation = f'{plan_id}-{index}'
                prior = ws.registry.find_change(operation, ws.session)
                while prior and prior['status'] == 'aborted':
                    operation = hashlib.sha256(operation.encode()).hexdigest()
                    prior = ws.registry.find_change(operation, ws.session)
                if prior and prior['status'] == 'committed':
                    row.update(status='applied', change_id=operation)
                elif prior:
                    row.update(status='conflict', reason='unresolved_operation')
                elif current != item['version']:
                    row.update(status='conflict', reason='version_mismatch')
                else:
                    row['status'] = 'skipped'
                    pending.append((item, operation, row))
            except (OSError, ValueError, ConnectorUnavailable) as error:
                row.update(status='failed', reason=str(error))
        if any(row['status'] in ('conflict', 'failed') for row in results):
            return self.result(results, blocked=True)
        for item, operation, row in pending:
            try:
                result = ws.write_bytes(item['path'], item['content'].encode(),
                                        expected=item['version'], operation_id=operation)
            except (OSError, ValueError, ConnectorUnavailable) as error:
                conflict = isinstance(error, ValueError) and str(error).startswith('版本冲突')
                row.update(status='conflict' if conflict else 'failed', reason=str(error))
                break
            row.update(status='applied', change_id=result['change_id'])
        return self.result(results, blocked=False)

    @staticmethod
    def result(rows, *, blocked):
        applied = sum(row['status'] == 'applied' for row in rows)
        status = 'applied' if applied == len(rows) else 'partial' if applied else 'blocked' if blocked else 'failed'
        return {'status': status, 'files': rows}
