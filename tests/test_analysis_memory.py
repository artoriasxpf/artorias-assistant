import asyncio
import json

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
            if isinstance(message, ToolMessage) and message.name == 'analysis_next_batch':
                if message.content.startswith('{'):
                    batch = json.loads(message.content)
                    break
        names = [item.name for item in messages if isinstance(item, ToolMessage)]
        if batch:
            assert batch['results'][0]['content'] == '示例文件\n'
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
                'name': 'analysis_next_batch', 'id': 'read-batch',
                'args': {'purpose': '读取示例', 'reads': [{'path': 'example.txt'}]},
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
    from artorias_server.analysis.repository import AnalysisRepository
    from artorias_server.analysis.models import BatchInput, NoteInput

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
    request = BatchInput(purpose='先前批次', reads=[{'path': 'example.txt'}])
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
                if kind == 'tool.result' and result['name'] == 'analysis_next_batch':
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
