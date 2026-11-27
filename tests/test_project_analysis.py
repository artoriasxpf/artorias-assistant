import asyncio
import json

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from artorias_local_app.bootstrap import create_engine, create_workspace
from artorias_server.storage.sqlite import Store
from artorias_server.tools.registry import build_tools


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / 'project'
    root.mkdir()
    monkeypatch.setenv('ARTORIAS_WORKSPACE', str(root))
    data = tmp_path / 'data'
    data.mkdir()
    store = Store(data / 'app.sqlite')
    session = store.session()
    store.submit(session, '分析项目文件', 'request')
    run = store.claim()
    workspace = create_workspace(store, session, run['id'])
    tools = {tool.name: tool for tool in build_tools(workspace, lambda *_: None)}
    return root, store, run, workspace, tools


def test_inventory_search_resume_and_read_versions(project):
    root, _, _, _, tools = project
    (root / 'a.txt').write_text('第一行\n目标 一\n目标 二\n', encoding='utf-8')
    (root / 'b.bin').write_bytes(bytes(1) + '目标'.encode())
    (root / '.hidden').write_text('目标')
    (root / 'link').symlink_to(root / 'a.txt')
    (root / 'node_modules').mkdir()
    (root / 'node_modules' / 'c.txt').write_text('目标')
    inventory = tools['project_inventory'].invoke({'max_entries': 2})
    assert inventory['coverage']['read_bytes'] == 0
    assert inventory['next_cursor']
    resumed = tools['project_inventory'].invoke({'max_entries': 2, 'cursor': inventory['next_cursor']})
    assert resumed['coverage']['read_bytes'] == 0
    assert tools['project_inventory'].invoke({'max_entries': 3, 'cursor': inventory['next_cursor']})['stop_reason'] == 'cursor_expired'

    args = {'inventory_id': inventory['inventory_id'], 'query': '目标.*', 'mode': 'regex', 'max_hits': 1}
    first = tools['project_search'].invoke(args)
    second = tools['project_search'].invoke({**args, 'cursor': first['next_cursor']})
    assert [(r['path'], r['line']) for page in (first, second) for r in page['hits']] == [('a.txt', 2), ('a.txt', 3)]
    items = [{'path': 'a.txt', 'line_start': 2, 'line_count': 1},
             {'path': 'a.txt', 'expected_version': 'old'}, {'path': 'b.bin'}, {'path': 'missing'}]
    result = tools['project_read_many'].invoke({'items': items})
    assert [row['status'] for row in result['items']] == ['ok', 'stale', 'binary', 'not_found']
    assert result['items'][0]['content'] == '目标 一\n'
    assert result['items'][0]['next_start'] == 3
    assert result['coverage']['read_bytes'] == (root / 'a.txt').stat().st_size + (root / 'b.bin').stat().st_size
    assert tools['project_write'].invoke({'path': 'a.txt', 'content': '重写'})['version']


def test_cjk_byte_budget_and_strict_schema(project):
    root, _, _, _, tools = project
    (root / 'x.txt').write_text('中' * 8000)
    items = [{'path': 'x.txt', 'limit': 8000}] * 8
    result = tools['project_read_many'].invoke({'items': items, 'max_output_bytes': 4096})
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= 4096
    lengths = [row['count'] for row in result['items']]
    assert min(lengths) > 0
    assert max(lengths) - min(lengths) <= 2
    with pytest.raises(ValidationError):
        tools['project_read_many'].invoke({'items': [{'path': 'x.txt', 'line_start': 1, 'offset': 0}]})


def test_revoked_directory_cannot_return_cached_page(project, tmp_path):
    _, store, _, workspace, tools = project
    outside = tmp_path / 'outside'
    outside.mkdir()
    for name in ['a', 'b']:
        (outside / name).write_text(name)
    grant = workspace.grant_access(str(outside), 'read')
    first = tools['project_inventory'].invoke({'root': str(outside), 'max_entries': 1})
    with store.tx() as db:
        db.execute('DELETE FROM workspace_grants WHERE location_id=? AND root=?', (grant['location_id'], str(outside)))
    with pytest.raises(PermissionError):
        tools['project_inventory'].invoke({'root': str(outside), 'max_entries': 1, 'cursor': first['next_cursor']})


class ReplayModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def response(name, args, index):
    return AIMessage(content='', tool_calls=[{'name': name, 'args': args, 'id': f't{index}'}])


def collect(engine, run):
    async def events():
        return [event async for event in engine.execute(run)]
    return asyncio.run(events())


@pytest.mark.parametrize('choice', ['approve', 'remember', 'reject'])
def test_batch_approval_and_metrics_one_graph_interrupt(project, tmp_path, choice):
    root, store, run, _, _ = project
    (root / 'known.txt').write_text('known')
    outside = tmp_path / 'external'
    outside.mkdir()
    paths = [outside / 'a.txt', outside / 'b.txt']
    for path in paths:
        path.write_text(path.name)
    model = ReplayModel(disable_streaming=True, messages=iter([
        response('project_read_many', {'items': [{'path': str(path)} for path in [*paths, 'known.txt']]}, 0),
        AIMessage(content='结束'),
    ]))
    engine = create_engine(tmp_path / 'data', store, model_factory=lambda: model)
    first = collect(engine, run)
    kind, approval = first[-1]
    assert kind == 'approval.requested'
    request, = approval['requests']
    assert len(request['permission_request_ids']) == 2
    store.finish(run, 'waiting', approval=approval)
    store.decide(run['id'], choice)
    second = collect(engine, store.claim())
    assert second[-1] == ('assistant.final', {'text': '结束'})
    tool_result = json.loads(next(payload['content'] for kind, payload in second if kind == 'tool.result'))
    expected = 'permission_denied' if choice == 'reject' else 'ok'
    assert [row['status'] for row in tool_result['items']] == [expected, expected, 'ok']
    metrics = [json.loads(e['payload']) for e in store.events(run['session'], 0) if e['kind'] == 'runtime.graph_metrics']
    assert len(metrics) == 2
    assert len({m['invocation_id'] for m in metrics}) == 2
    assert all(m['graph_steps'] > 0 and m['model_requests'] == m['estimated_requests'] == 1 for m in metrics)
    assert all(m['request_bytes'] > 0 for m in metrics)


def test_replay_24_known_paths_compares_model_rounds(project, tmp_path):
    root, store, run, _, _ = project
    paths = [f'{n:02d}.txt' for n in range(24)]
    for path in paths:
        (root / path).write_text(f'fact: {path}')
    model = ReplayModel(disable_streaming=True, messages=iter([
        *[response('project_read_many', {'items': [{'path': path} for path in paths[i:i + 8]]}, i)
          for i in range(0, len(paths), 8)],
        AIMessage(content='已读取 24 个文件'),
    ]))
    events = collect(create_engine(tmp_path / 'data', store, model_factory=lambda: model), run)
    rows = [row for kind, payload in events if kind == 'tool.result'
            for row in json.loads(payload['content'])['items']]
    assert [row['path'] for row in rows if row['status'] == 'ok'] == paths
    assert sum(kind == 'tool.call' for kind, _ in events) == 3

    store.finish(run, 'completed', '已读取 24 个文件')
    store.submit(run['session'], '分析项目文件', 'baseline')
    baseline = store.claim()
    old_batches = [
        AIMessage(content='', tool_calls=[
            response('project_read', {'path': path}, index).tool_calls[0]
            for index, path in enumerate(paths[start:start + 3], start=start)
        ])
        for start in range(0, len(paths), 3)
    ]
    old_model = ReplayModel(disable_streaming=True, messages=iter([
        *old_batches, AIMessage(content='已读取 24 个文件'),
    ]))
    old_events = collect(create_engine(tmp_path / 'data', store, model_factory=lambda: old_model), baseline)
    assert [json.loads(payload['content'])['path'] for kind, payload in old_events if kind == 'tool.result'] == paths
    old, new = [
        next(json.loads(event['payload']) for event in store.events(run['session'], 0)
             if event['kind'] == 'runtime.graph_metrics' and event['run'] == current['id'])
        for current in (baseline, run)
    ]
    assert (old['model_requests'], new['model_requests']) == (9, 4)
    assert old['graph_steps'] > new['graph_steps']


def test_managed_cloud_search_uses_bound_workspace(project):
    from artorias_server.workspace.catalog import WorkspaceCatalog

    root, store, _, _, _ = project
    catalog = WorkspaceCatalog(store)
    cloud = catalog.register('云端', 'cloud-project', 'cloud_storage')
    session = store.session()
    catalog.select(session, cloud['id'])
    workspace = create_workspace(store, session, 'cloud-run')
    workspace.write('cloud.txt', '目标云端', expected=None)
    (root / 'local.txt').write_text('目标本地')
    tools = {tool.name: tool for tool in build_tools(workspace, lambda *_: None)}
    result = tools['project_search'].invoke({'query': '目标'})
    assert [hit['path'] for hit in result['hits']] == ['cloud.txt']


def test_search_reports_unsupported_endpoint(project):
    _, _, _, workspace, tools = project

    class EndpointWithoutAnalysis:
        pass

    workspace.analysis_root = lambda path: (EndpointWithoutAnalysis(), '.', ('test', path))
    assert tools['project_search'].invoke({'query': 'hello'})['stop_reason'] == 'unsupported'


def test_first_phase_tools_can_be_disabled(project, monkeypatch):
    _, _, _, workspace, _ = project
    monkeypatch.setenv('ARTORIAS_PROJECT_ANALYSIS', '0')
    names = {tool.name for tool in build_tools(workspace, lambda *_: None)}
    assert 'project_read' in names
    assert names.isdisjoint({'project_inventory', 'project_search', 'project_read_many'})


def test_hidden_inventory_and_search_across_scan_modes(tmp_path, monkeypatch):
    from artorias_local_app.bootstrap import create_workspace
    from artorias_server.storage.sqlite import Store
    from artorias_server.tools.registry import build_tools

    root = tmp_path / 'project'
    monkeypatch.setenv('ARTORIAS_WORKSPACE', str(root))
    (tmp_path / 'data').mkdir()
    store = Store(tmp_path / 'data' / 'app.sqlite')
    ws = create_workspace(store, store.session())
    ws.write('nested/.hidden.txt', 'needle')
    tools = {item.name: item.invoke for item in build_tools(ws, lambda *_: None)}
    for snapshot in (False, True):
        args = {'snapshot_page': snapshot, 'query': 'needle'}
        assert tools['project_search'](args)['hits'] == []
        assert tools['project_search']({**args, 'include_hidden': True})['hits']
        page = tools['project_inventory']({'snapshot_page': snapshot, 'include_hidden': True})
        assert page['filters']['include_hidden'] is True
        assert 'hidden' not in page['coverage']['skipped']
