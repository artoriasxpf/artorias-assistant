import asyncio
import json

import pytest

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage

from artorias_local_app.bootstrap import create_engine, create_workspace
from artorias_server.scheduler.service import Scheduler
from artorias_server.storage.sqlite import Store
from artorias_server.tasks.executor import TaskRuntime
from artorias_server.tasks.models import Plan
from task_fixtures import finish_message, plan_item


class AnalysisModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, *args, **kwargs):
        batch = None
        for message in reversed(messages):
            if isinstance(message, ToolMessage) and message.name == 'project_read':
                if message.content.startswith('{'):
                    batch = json.loads(message.content)
                    break
        names = [item.name for item in messages if isinstance(item, ToolMessage)]
        if batch:
            assert batch['content'] == '示例文件\n'
            reply = AIMessage(content='', tool_calls=[{
                'name': 'record_analysis_note', 'id': 'save-note',
                'args': {'batch_id': batch['batch_id'], 'summary': '找到示例', 'findings': [
                    {'text': '文件包含示例', 'refs': [0]},
                ]},
            }])
        elif 'record_analysis_note' in names:
            reply = finish_message('已核对示例文件')
        else:
            reply = AIMessage(content='', tool_calls=[{
                'name': 'project_read', 'id': 'read-batch',
                'args': {'path': 'example.txt', 'unit': 'byte'},
            }])
        return self._reply(reply, messages, *args, **kwargs)

    def _reply(self, reply, messages, *args, **kwargs):
        from langchain_core.outputs import ChatGeneration, ChatResult
        return ChatResult(generations=[ChatGeneration(message=reply)])


def test_analysis_pipeline_commits_actual_presented_range(tmp_path, monkeypatch):
    monkeypatch.setenv('ARTORIAS_WORKSPACE', str(tmp_path / 'project'))
    data = tmp_path / 'data'
    data.mkdir()
    store = Store(data / 'app.sqlite')
    session = store.session()
    store.submit(session, '分析 example.txt', 'run')
    run = store.claim()
    workspace = create_workspace(store, session, run['id'])
    workspace.write('example.txt', '示例文件\n')

    async def plan(_):
        return Plan(kind='tasks', items=[plan_item(key='review', goal='分析示例', analysis={
            'analysis_type': 'document_file', 'intent': 'summary', 'goal': '分析示例',
            'roots': ['example.txt'],
        })])

    model = AnalysisModel(messages=iter([]), disable_streaming=True)
    runtime = TaskRuntime(data, store, engine=create_engine(data, store, model_factory=lambda: model), planner=plan)
    asyncio.run(Scheduler(store, runtime).execute(run))
    assert store.query('SELECT status,result FROM task_steps') == [
        {'status': 'delivered', 'result': '已核对示例文件'},
    ], store.query('SELECT kind,payload FROM events')
    assert store.query('SELECT state FROM analysis_batches') == [{'state': 'committed'}]
    assert store.query('SELECT start,end,unit FROM analysis_ranges') == [
        {'start': 0, 'end': len('示例文件\n'.encode()), 'unit': 'byte'},
    ]
    assert store.query('SELECT version FROM analysis_summaries') == [{'version': 1}]


def test_recovered_step_reuses_committed_note_and_replays_pending_batch(tmp_path, monkeypatch):
    from contextlib import aclosing
    from artorias_server.tools.analysis.repository import AnalysisRepository
    from artorias_server.tools.analysis.models import NoteInput

    monkeypatch.setenv('ARTORIAS_WORKSPACE', str(tmp_path / 'project'))
    data = tmp_path / 'data'
    data.mkdir()
    store = Store(data / 'app.sqlite')
    session = store.session()
    store.submit(session, '分析 example.txt', 'run')
    run = store.claim()
    workspace = create_workspace(store, session, run['id'])
    workspace.write('example.txt', '示例文件\n')
    plan = Plan(kind='tasks', items=[plan_item(key='review', goal='分析示例', analysis={
        'analysis_type': 'document_file', 'intent': 'summary', 'goal': '分析示例',
        'roots': ['example.txt'],
    })])

    async def planner(_):
        return plan

    model = AnalysisModel(messages=iter([]), disable_streaming=True)
    runtime = TaskRuntime(data, store, engine=create_engine(data, store, model_factory=lambda: model), planner=planner)
    runtime.repo.install(run, plan, [])
    step = store.query('SELECT * FROM task_steps')[0]
    repo = AnalysisRepository(store)
    analysis = repo.attach(dict(run, task_id=step['task']), plan.items[0].analysis,
                           workspace.describe()['id'], 'review')
    # One committed batch from a prior model invocation, with provenance.
    request = {'tool': 'project_read', 'args': {'path': 'example.txt', 'unit': 'byte'}}
    batch = repo.plan(analysis['id'], 'first', request)
    identity = workspace.describe()
    receipt = [{'requested_path': 'example.txt', 'path': str(tmp_path / 'project' / 'example.txt'),
                'location_id': identity['location_id'], 'workspace_id': identity['id'],
                'directory': False, 'status': 'ok', 'kind': 'content', 'version': 'older',
                'unit': 'byte', 'start': 0, 'end': 3, 'total': 13}]
    payload = {'batch_id': batch['id']}
    repo.returned(batch['id'], receipt, payload)
    repo.presented(analysis['id'], payload)
    note = NoteInput(batch_id=batch['id'], summary='先前的结论')
    repo.commit(analysis['id'], note)
    repo.commit(analysis['id'], note)

    async def stop_after_read():
        async with aclosing(runtime.execute(run)) as events:
            async for kind, result in events:
                if kind == 'tool.result' and result['name'] == 'project_read':
                    return json.loads(result['content'])['batch_id']
        raise AssertionError('没有返回读取批次')

    pending_id = asyncio.run(stop_after_read())
    store = Store(data / 'app.sqlite')
    store.recover()
    resumed = store.claim()
    assert resumed['id'] == run['id']
    runtime = TaskRuntime(data, store, engine=create_engine(data, store, model_factory=lambda: model), planner=planner)
    asyncio.run(Scheduler(store, runtime).execute(resumed))
    assert store.query('SELECT id,state FROM analysis_batches ORDER BY ordinal') == [
        {'id': batch['id'], 'state': 'committed'}, {'id': pending_id, 'state': 'committed'},
    ]
    assert store.query('SELECT status FROM task_steps') == [{'status': 'delivered'}]


def test_large_utf8_range_and_stable_inventory_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv('ARTORIAS_WORKSPACE', str(tmp_path / 'project'))
    data = tmp_path / 'data'
    data.mkdir()
    store = Store(data / 'app.sqlite')
    workspace = create_workspace(store, store.session())
    root = tmp_path / 'project'
    root.mkdir(exist_ok=True)
    path = root / 'large.log'
    content = '汉字一行\n'.encode()
    with path.open('wb') as stream:
        stream.write(content)
        stream.truncate(100 * 1024 * 1024)
    endpoint, relative, _, _ = workspace._resolve('large.log', 'read')
    first = endpoint.analysis_range(relative, offset=0, limit=4)
    second = endpoint.analysis_range(relative, offset=first['next_start'], limit=4,
                                     expected_version=first['version'])
    assert first['content'] + second['content'] == '汉字'
    assert second['total'] == 100 * 1024 * 1024
    for name in ('a', 'b', 'c'):
        (root / name).touch()
    endpoint, relative, _ = workspace.analysis_root('.')
    page = endpoint.analysis_inventory_page(relative, request_id='page1', cursor=None, limit=2)
    (root / 'aa').touch()
    tail = endpoint.analysis_inventory_page(relative, request_id='page2', cursor=page['next_cursor'], limit=2)
    assert [item['path'] for item in page['entries'] + tail['entries']] == ['a', 'b', 'c', 'large.log']
    assert tail == endpoint.analysis_inventory_page(relative, request_id='page2', cursor=page['next_cursor'], limit=2)


def test_endpoint_wait_releases_job_and_retains_operation_id(tmp_path, monkeypatch):
    from artorias_device.workspace.local_workspace import LocalWorkspace

    monkeypatch.setenv('ARTORIAS_WORKSPACE', str(tmp_path / 'project'))
    data = tmp_path / 'data'
    data.mkdir()
    store = Store(data / 'app.sqlite')
    session = store.session()
    store.submit(session, '分析 example.txt', 'run')
    run = store.claim()
    workspace = create_workspace(store, session, run['id'])
    workspace.write('example.txt', '示例文件\n')
    read_range = LocalWorkspace.analysis_range
    requests = []

    def pending_once(self, path, *, request_id, **options):
        requests.append(request_id)
        if len(requests) == 1:
            return {'status': 'pending', 'operation_id': 'device-operation'}
        return read_range(self, path, **options)

    monkeypatch.setattr(LocalWorkspace, 'analysis_range', pending_once)
    monkeypatch.setattr(LocalWorkspace, 'analysis_range_async', True, raising=False)

    async def plan(_):
        return Plan(kind='tasks', items=[plan_item(key='review', goal='分析示例', analysis={
            'analysis_type': 'document_file', 'intent': 'summary', 'goal': '分析示例',
            'roots': ['example.txt'],
        })])

    model = AnalysisModel(messages=iter([]), disable_streaming=True)
    runtime = TaskRuntime(data, store, engine=create_engine(data, store, model_factory=lambda: model), planner=plan)
    scheduler = Scheduler(store, runtime)
    asyncio.run(scheduler.execute(run))
    assert store.query('SELECT status FROM runs') == [{'status': 'analysis_waiting'}]
    assert store.query('SELECT state,dependency_id FROM analysis_executions') == [
        {'state': 'waiting', 'dependency_id': 'device-operation'},
    ]
    assert store.claim() is None
    with store.tx() as db:
        db.execute('UPDATE analysis_executions SET ready_at=0')
    resumed = store.claim()
    assert resumed['id'] == run['id']
    assert store.claim() is None
    asyncio.run(scheduler.execute(resumed))
    assert len(requests) == 2 and requests[0] == requests[1]
    assert store.query('SELECT status FROM runs') == [{'status': 'completed'}]


def test_search_pages_replay_within_a_file(tmp_path, monkeypatch):
    monkeypatch.setenv('ARTORIAS_WORKSPACE', str(tmp_path / 'project'))
    data = tmp_path / 'data'
    data.mkdir()
    store = Store(data / 'app.sqlite')
    workspace = create_workspace(store, store.session())
    workspace.write('hits.txt', 'needle\n' * 13)
    endpoint, relative, _ = workspace.analysis_root('.')
    first = endpoint.analysis_search_page(relative, request_id='a', cursor=None, query='needle')
    second = endpoint.analysis_search_page(relative, request_id='b', cursor=first['next_cursor'], query='needle')
    assert [hit['line'] for hit in first['hits'] + second['hits']] == list(range(1, 14))
    assert second['next_cursor'] is None
    assert first == endpoint.analysis_search_page(relative, request_id='a', cursor=None, query='needle')
    other = endpoint.analysis_search_page(relative, request_id='c', cursor=first['next_cursor'], query='other')
    assert other['status'] == 'cursor_expired'


def test_analysis_policy_and_project_receipts(tmp_path, monkeypatch):
    from artorias_server.permissions import tool_call
    from artorias_server.tools.analysis.service import AnalysisSession
    from artorias_server.tools.policy import READ_TOOLS, step_tools
    from artorias_server.tools.registry import build_tools

    monkeypatch.setenv('ARTORIAS_WORKSPACE', str(tmp_path / 'project'))
    (tmp_path / 'data').mkdir()
    store = Store(tmp_path / 'data' / 'state.sqlite')
    session = store.session()
    store.submit(session, '分析目录', 'run')
    run = store.claim()
    ws = create_workspace(store, session, run['id'])
    ws.write('example.txt', 'first\nsecond\n')
    analysis = AnalysisSession(store, dict(run, step_key='inspect', task_id='task', analysis_plan={
        'analysis_type': 'document_directory', 'intent': 'summary', 'goal': '核对目录', 'roots': ['.'],
    }), ws)
    tools = build_tools(ws, lambda *_: None)
    assert {t.name for t in step_tools(tools, None, True)} == READ_TOOLS | {'project_change_plan'}
    mixed = step_tools(tools, ['project_write'], True)
    assert 'project_write' in {t.name for t in mixed}
    assert 'run_python' not in {t.name for t in mixed}
    bound = {t.name: t for t in analysis.bind_project_tools(mixed) + analysis.tools()}
    cases = [
        ('project_inventory', {'root': '.', 'snapshot_page': True}, 'metadata'),
        ('project_search', {'query': 'first', 'snapshot_page': True}, 'snippet'),
        ('project_tree', {}, 'metadata'),
        ('project_list', {}, 'metadata'),
        ('project_read', {'path': 'example.txt'}, 'content'),
        ('project_read_many', {'items': [{'path': 'example.txt', 'line_start': 2}]}, 'content'),
    ]
    for i, (name, args, kind) in enumerate(cases):
        token = tool_call.set(str(i))
        try:
            result = bound[name].invoke(args)
            with pytest.raises(ValueError, match='先记录'):
                bound['project_tree'].invoke({})
            analysis.repo.presented(analysis.id, result)
            bound['record_analysis_note'].invoke({'batch_id': result['batch_id'], 'summary': name})
            row = json.loads(analysis.repo.notes(analysis.id, after=i)[0]['body_json'])['receipt'][0]
            assert (row['kind'], row['status']) == (kind, 'ok')
        finally:
            tool_call.reset(token)
    assert analysis.snapshot()['coverage']['full']
    assert len(bound['read_analysis_notes'].invoke({'after': 2})['notes']) == 4


@pytest.mark.parametrize("interrupt_after_write", [False, True])
def test_mixed_analysis_writes_once_after_read_and_note(tmp_path, monkeypatch, interrupt_after_write):
    monkeypatch.setenv('ARTORIAS_WORKSPACE', str(tmp_path / 'project'))
    data = tmp_path / 'data'
    data.mkdir()
    store = Store(data / 'app.sqlite')
    session = store.session()
    store.submit(session, '核验文件并保存报告', 'run')
    run = store.claim()
    ws = create_workspace(store, session, run['id'])
    ws.write('example.txt', '示例文件\n')

    class ReportModel(AnalysisModel):
        def _generate(self, messages, *args, **kwargs):
            last = messages[-1]
            if not isinstance(last, ToolMessage) or last.name == 'project_read':
                return super()._generate(messages, *args, **kwargs)
            if last.name == 'record_analysis_note':
                call = {'name': 'read_analysis_notes', 'id': 'get-notes', 'args': {}}
            elif last.name == 'read_analysis_notes':
                note = json.loads(last.content)['notes'][0]['note']
                call = {'name': 'project_write', 'id': 'save-report',
                        'args': {'path': 'report.txt', 'content': note['summary']}}
            else:
                assert last.name == 'project_write' and last.status == 'success'
                return self._reply(finish_message('报告已保存'), messages, *args, **kwargs)
            return self._reply(AIMessage(content='', tool_calls=[call]), messages, *args, **kwargs)

    async def planner(_):
        item = plan_item(key='review', goal='保存报告', analysis={
            'analysis_type': 'document_file', 'intent': 'summary', 'goal': '核验', 'roots': ['example.txt'],
        })
        return Plan(kind='tasks', items=[item.model_copy(update={'allowed_tool_groups': ['project_write']})])

    model = ReportModel(messages=iter([]), disable_streaming=True)
    runtime = TaskRuntime(data, store, engine=create_engine(data, store, model_factory=lambda: model), planner=planner)
    if interrupt_after_write:
        from contextlib import aclosing

        async def stop_after_write():
            async with aclosing(runtime.execute(run)) as events:
                async for kind, detail in events:
                    if kind == 'tool.result' and detail['name'] == 'project_write':
                        return
            pytest.fail('没有写入报告')

        asyncio.run(stop_after_write())
        store.recover()
        assert store.query('SELECT status FROM runs') == [{'status': 'interrupted'}]
        assert store.claim() is None
    else:
        asyncio.run(Scheduler(store, runtime).execute(run))
        assert store.query('SELECT status FROM runs') == [{'status': 'completed'}], store.events(session, 0)
    assert (tmp_path / 'project' / 'report.txt').read_text() == '找到示例'
