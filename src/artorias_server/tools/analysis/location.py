"""Resolve a file reference before creating durable byte ranges."""
import json
import re
from pathlib import Path, PurePosixPath
from typing import TypedDict

from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from artorias_server.permissions import MissingPermission, tool_call
from artorias_server.tools.executor import WorkspaceToolExecutor
from artorias_server.tools.project_analysis import ProjectAnalysis, SearchArguments
from artorias_shared.workspace import ConnectorUnavailable


class MissingSourcePath(Exception):
    pass


class LocationReady(Exception):
    pass


class LocationState(TypedDict, total=False):
    scopes: list[str]
    path: str
    searches: list[dict]


# Use paths from the user's messages. Tool output and guessed paths do not grant
# permission or introduce new directories to search outside the workspace.
ABSOLUTE_PATH = re.compile(r'[`"\'](/[^\n`"\']+)[`"\']|(?<![\w:/])(/[^\s`"\'，。；：、<>]+)')


def explicit_reference(reference, request):
    name = PurePosixPath(reference).name
    matches = {quoted or bare for quoted, bare in ABSOLUTE_PATH.findall(request)
               if PurePosixPath(quoted or bare).name == name}
    return matches.pop() if len(matches) == 1 else reference


def candidate_scopes(reference, messages):
    ref = PurePosixPath(reference)
    scopes = []
    for message in messages:
        for quoted, bare in ABSOLUTE_PATH.findall(message):
            path = PurePosixPath(quoted or bare)
            if '..' in path.parts or len(path.parts) < 3:
                continue
            is_target = path.name == ref.name
            # A named parent from the proposed relative path, or a previously
            # mentioned directory when the user referred to a bare filename.
            if not (is_target or path.name in ref.parts[:-1] or len(ref.parts) == 1):
                continue
            scope = str(path.parent if is_target else path)
            if scope not in scopes:
                scopes.append(scope)
    return scopes[:3]


def probe(workspace, path):
    endpoint, relative, _, _ = workspace._resolve(path, 'read')
    observe = getattr(endpoint, 'analysis_observation', None)
    if observe:
        observe(relative)
    elif endpoint.head(relative) is None:
        raise FileNotFoundError(path)
    return path


def find_in_scope(workspace, scope, reference):
    name = PurePosixPath(reference).name
    args = SearchArguments(root=scope, mode='name', query=name)
    candidates = {}
    incomplete = False
    analysis = ProjectAnalysis(workspace, WorkspaceToolExecutor(workspace).files)
    for _ in range(3):
        page = analysis.search(args, None)
        for hit in page.get('hits', []):
            path = hit['path']
            if PurePosixPath(path).name != name:
                continue
            try:
                probe(workspace, path)
                candidates[workspace.path(path)] = path
            except (FileNotFoundError, NotADirectoryError, IsADirectoryError, MissingPermission):
                continue
        cursor = page.get('next_cursor')
        skipped = page.get('coverage', {}).get('skipped', {})
        incomplete |= any(skipped.get(key) for key in ('depth_limit', 'unreadable', 'path_limit'))
        if len(candidates) >= 5:
            incomplete |= bool(cursor)
            break
        if not cursor:
            incomplete |= page.get('stop_reason') != 'complete'
            break
        args = args.model_copy(update={'cursor': cursor})
    else:
        incomplete = True
    return dict(
        root=scope, candidates=list(candidates.values())[:5],
        status='incomplete' if incomplete else 'searched',
    )


def search_scope(workspace, root, reference, approve, call_id, *, allow_approval, preferred=None):
    approval_attempted = False

    def search():
        if preferred:
            try:
                return dict(root=root, status='searched', candidates=[probe(workspace, preferred)])
            except (MissingPermission, FileNotFoundError, NotADirectoryError, IsADirectoryError):
                pass
        try:
            return find_in_scope(workspace, root, reference)
        except (FileNotFoundError, NotADirectoryError):
            return dict(root=root, status='missing')

    try:
        try:
            return search()
        except MissingPermission:
            if not allow_approval:
                return dict(root=root, status='permission_required')
            approval_attempted = True
            grant = WorkspaceToolExecutor(workspace).authorize(root, 'read', approve, call_id)
            if grant.get('rejected'):
                return dict(root=root, status='denied', approval_attempted=True)
            return dict(search(), approval_attempted=True)
    except (MissingPermission, ValueError, KeyError, OSError, ConnectorUnavailable) as error:
        return dict(root=root, status='unavailable', reason=str(error)[:300],
                    approval_attempted=approval_attempted)


def parent_scope(reference):
    parent = reference.rsplit('/', 1)[0]
    if parent.startswith('workspace://') and '/' not in parent[len('workspace://'):]:
        return parent + '/'
    return parent


def suggested_scopes(workspace, reference, scopes):
    anchors = [*scopes]
    if reference.startswith('/'):
        anchors.append(parent_scope(reference))
    description = workspace.describe()
    if description['kind'] == 'device_fs':
        anchors.append(description['root'])
    suggestions = []
    home = PurePosixPath(Path.home())
    for anchor in anchors:
        if not anchor.startswith('/'):
            continue
        path = PurePosixPath(anchor)
        for folder in (path, *list(path.parents)[:2]):
            if len(folder.parts) < 3 or folder == home or folder in home.parents:
                continue
            if str(folder) not in suggestions:
                suggestions.append(str(folder))
    return suggestions


async def preflight(workspace, checkpoint_provider, thread_id, reference, scopes, decision):
    """Try known paths, then return observations for the location unit."""
    explicit = reference.startswith(('/', 'workspace://'))
    if explicit:
        ancestors = [scope for scope in scopes if reference.startswith('/')
                     and PurePosixPath(scope) in PurePosixPath(reference).parents]
        scopes = list(dict.fromkeys([parent_scope(reference), *ancestors]))
        scopes = [scope for scope in scopes if scope and scope != '/']
    else:
        scopes = [*scopes, '.']

    def locate(state):
        searches = []
        # When the model supplied a relative guess, a user-mentioned directory
        # has priority over the current workspace.
        if explicit:
            try:
                return {'path': probe(workspace, reference)}
            except (MissingPermission, ValueError, KeyError, OSError, ConnectorUnavailable):
                pass
        for scope in state['scopes']:
            preferred = reference if scope == '.' or (explicit and scope == parent_scope(reference)) else None
            ref_parts = PurePosixPath(reference).parts
            folder = PurePosixPath(scope)
            if not explicit and folder.name in ref_parts[:-1]:
                preferred = str(folder.joinpath(*ref_parts[ref_parts.index(folder.name) + 1:]))
            allow_approval = scope.startswith('/') and not any(
                result.get('approval_attempted') for result in searches)
            result = search_scope(workspace, scope, reference, interrupt, thread_id,
                                  allow_approval=allow_approval, preferred=preferred)
            searches.append(result)
            paths = result.get('candidates', [])
            if result['status'] == 'searched' and len(paths) == 1:
                return {'path': paths[0], 'searches': searches}
            if paths:
                break
        return {'searches': searches}

    builder = StateGraph(LocationState)
    builder.add_node('locate', locate)
    builder.add_edge(START, 'locate')
    builder.add_edge('locate', END)
    async with checkpoint_provider.open() as saver:
        graph = builder.compile(checkpointer=saver)
        config = {'configurable': {'thread_id': thread_id}}
        state = await graph.aget_state(config)
        used_decision = bool(state.interrupts and decision)
        if used_decision or (not state.interrupts and not state.values):
            action = Command(resume=decision) if used_decision else {'scopes': scopes}
            await graph.ainvoke(action, config)
            state = await graph.aget_state(config)
        pending = [{**item.value, 'request_id': item.id, 'key': item.value.get('key', item.value['kind'])}
                   for item in state.interrupts]
        return state.values, pending, used_decision


def save_location(db, unit, state):
    db.execute("UPDATE work_units SET inputs=? WHERE id=? AND status='running'",
               (json.dumps(state, ensure_ascii=False), unit))


class FileLocation(AgentMiddleware):
    """A persisted file search with tool-backed completion evidence."""

    def __init__(self, store, workspace, unit, attempt):
        self.store = store
        self.workspace = workspace
        self.unit = unit
        self.attempt = attempt
        self.reference = json.loads(unit['inputs'])['reference']

    def state(self, db=None):
        sql = 'SELECT inputs FROM work_units WHERE id=?'
        args = (self.unit['id'],)
        row = db.execute(sql, args).fetchone() if db is not None else self.store.query(sql, args)[0]
        return json.loads(row['inputs'])

    def save(self, db, state):
        save_location(db, self.unit['id'], state)

    def start_attempt(self):
        with self.store.tx() as db:
            state = self.state(db)
            interrupted = [item for item in state.get('searches', [])
                           if item['status'] == 'pending' and item['attempt_id'] != self.attempt]
            for item in interrupted:
                item['status'] = 'interrupted'
            if interrupted:
                self.save(db, state)
        return state

    def key(self, root):
        space, target = self.workspace._target(root, directory=True)
        return f"{space['location_id']}:{target}"

    async def awrap_model_call(self, request, handler):
        if self.state().get('selection'):
            raise LocationReady
        return await handler(request)

    def bind_tools(self):
        @tool
        def search_for_file(root: str) -> dict:
            """Search one directory for the requested exact filename. Use an authorized directory, a suggested absolute directory, or a more specific directory within a suggested root. Missing read access may prompt for approval. Search one directory at a time."""
            if len(root) > 1024:
                raise ValueError('目录路径过长。')
            key = self.key(root)
            call_id = tool_call.get()
            with self.store.tx() as db:
                state = self.state(db)
                path = PurePosixPath(root)
                permitted = path.is_absolute() and any(
                    path.is_relative_to(PurePosixPath(suggested)) for suggested in state['suggested_roots']
                )
                for entry in [*state['preflight'], *state.get('searches', [])]:
                    if entry['root'] == root or entry.get('key') == key:
                        if entry['status'] == 'pending' and entry['call_id'] == call_id:
                            break
                        if entry['status'] == 'pending':
                            raise ValueError('该目录的搜索仍在等待结果。')
                        if entry['status'] == 'interrupted':
                            continue
                        if entry['status'] == 'permission_required' and permitted and 'key' not in entry:
                            continue
                        return {k: v for k, v in entry.items() if k not in ('key', 'call_id', 'attempt_id')}
                else:
                    if state.get('selection') or len(state.get('searches', [])) >= 4:
                        raise ValueError('定位搜索次数已到，请提交已验证的路径或向用户确认。')
                    if any(item['status'] == 'pending' for item in state.get('searches', [])):
                        raise ValueError('已有搜索正在执行。请等它返回后再选择下一步。')
                    entry = dict(root=root, key=key, call_id=call_id, attempt_id=self.attempt, status='pending')
                    state.setdefault('searches', []).append(entry)
                    self.save(db, state)
                if permitted:
                    permitted = not any(
                        old['status'] == 'denied' and old['root'].startswith('/')
                        and path.is_relative_to(PurePosixPath(old['root']))
                        for old in [*state['preflight'], *state.get('searches', [])]
                    )
            result = search_scope(self.workspace, root, self.reference, interrupt, call_id,
                                  allow_approval=permitted)
            with self.store.tx() as db:
                state = self.state(db)
                for item in state['searches']:
                    if item['call_id'] == call_id:
                        item.update(result)
                        break
                self.save(db, state)
            return result

        @tool
        def finish_file_location(path: str | None = None) -> dict:
            """Submit the unique verified match, or omit path to ask the user to clarify. Only submit a path returned by a completed search."""
            state = self.state()
            if state.get('selection') is not None:
                return state['selection']
            searches = [*state['preflight'], *state.get('searches', [])]
            if any(item['status'] == 'pending' for item in searches):
                raise ValueError('请等待搜索结果后再提交定位结论。')
            candidates = self.candidates(searches)
            if path is not None:
                if len(candidates) != 1 or not any(path in aliases for aliases in candidates.values()):
                    raise ValueError('只能提交工具核实的唯一候选文件。若有多个不同候选，请向用户确认。')
                if not any(path in item.get('candidates', []) and item['status'] == 'searched'
                           for item in searches):
                    raise ValueError('搜索范围尚未核实，请缩小范围重新搜索或向用户确认。')
                selection = {'path': path}
            else:
                selection = self.clarification(state, candidates)
            with self.store.tx() as db:
                if state != self.state(db):
                    raise ValueError('搜索记录已变化，请根据最新结果提交。')
                state['selection'] = selection
                self.save(db, state)
            return selection

        return [search_for_file, finish_file_location]

    def candidates(self, searches):
        candidates = {}
        for result in searches:
            for path in result.get('candidates', []):
                try:
                    probe(self.workspace, path)
                    candidates.setdefault(self.workspace.path(path), set()).add(path)
                except (MissingPermission, ValueError, KeyError, OSError, ConnectorUnavailable):
                    continue
        return candidates

    def clarification(self, state, candidates=None):
        searches = [*state['preflight'], *state.get('searches', [])]
        if candidates is None:
            candidates = self.candidates(searches)
        paths = [sorted(aliases)[0] for aliases in candidates.values()]
        message = f'未能确认 {PurePosixPath(self.reference).name} 的位置。'
        if paths:
            message += '候选路径：' + '、'.join(paths[:5]) + '。'
        checked = [item['root'] for item in searches if item['status'] in ('searched', 'incomplete', 'missing')]
        if checked:
            message += '已检查：' + '、'.join(dict.fromkeys(checked)) + '。'
        denied = [item['root'] for item in searches if item['status'] == 'denied']
        if denied:
            message += '未获读取权限：' + '、'.join(dict.fromkeys(denied)) + '。'
        unavailable = [item['root'] for item in searches if item['status'] == 'unavailable']
        if unavailable:
            message += '无法搜索：' + '、'.join(dict.fromkeys(unavailable)) + '。'
        return {'error': message + '请指定文件路径或提供更具体的搜索目录。'}
