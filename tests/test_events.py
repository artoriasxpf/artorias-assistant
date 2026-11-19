"""Event emission coverage for the session event table.

Covers the events added to close the gaps in docs/event_catalog.md and the
tool-failure boundary: tool call failures must come back as error tool results
instead of aborting the agent run.
"""
import asyncio
import json

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from artorias_server.storage.sqlite import Store
from artorias_server.scheduler.service import Scheduler
from artorias_server.tasks.executor import TaskRuntime
from artorias_server.tasks.models import Plan, Item, RequirementPatch, RequirementOperation
from artorias_server.tasks.repository import TaskRepository
from artorias_server.workspace.catalog import WorkspaceCatalog


class FakeModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def setup_task(tmp_path):
    db = Store(tmp_path/'app.sqlite')
    sid = db.session()
    db.submit(sid, '创建框架并写说明', 'one')
    return db, db.claim()


def test_tool_failure_returns_error_result(tmp_path, monkeypatch):
    """工具执行失败必须以 error tool result 返回，不能阻断 agent 循环。"""
    import artorias_server.runtime.service as rt
    replies = iter([
        AIMessage(content='', tool_calls=[{'name':'project_read','args':{'path':'missing.txt'},'id':'call_err','type':'tool_call'}]),
        AIMessage(content='文件不存在，已按错误处理'),
    ])
    monkeypatch.setenv('ARTORIAS_MODEL', 'fake')
    monkeypatch.setattr(rt, 'create_model', lambda: FakeModel(disable_streaming=True, messages=replies))
    db = Store(tmp_path/'app.sqlite')
    sid = db.session(); db.submit(sid, '读文件', 'k'); run = db.claim()
    from artorias_local_app.bootstrap import create_engine
    async def collect():
        return [e async for e in create_engine(tmp_path, db).execute(run)]
    events = asyncio.run(collect())
    result = next(p for k, p in events if k == 'tool.result')
    assert result['call_id'] == 'call_err'
    assert result['name'] == 'project_read'
    assert result['status'] == 'error'
    assert 'FileNotFoundError' in result['content']
    assert events[-1] == ('assistant.final', {'text': '文件不存在，已按错误处理'})
    relevant = [(kind, payload) for kind, payload in events if kind in (
        'model.started', 'model.finished', 'tool.call', 'tool.result',
    )]
    assert [kind for kind, _ in relevant] == [
        'model.started', 'model.finished', 'tool.call', 'tool.result', 'model.started', 'model.finished',
    ]
    first, second = relevant[0][1], relevant[4][1]
    assert first == relevant[1][1] and second == relevant[5][1]
    assert first['stage'] == second['stage'] == 'agent'
    assert first['request_id'] != second['request_id']


def test_cancel_waiting_run_closes_approval(tmp_path):
    """取消等待审批的 Run 时，写入 approval.closed 标记审批被撤销。"""
    db = Store(tmp_path/'app.sqlite')
    sid = db.session(); db.submit(sid, 'hi', 'k'); run = db.claim()
    db.finish(run, 'waiting', approval={
        'task_id': 't1',
        'requests': [{'request_id': 'req-1', 'key': 'code_execution', 'kind': 'code_execution'}],
    })
    scheduler = Scheduler(db, object())
    scheduler.cancel(run['id'])
    events = [(e['kind'], json.loads(e['payload'])) for e in db.events(sid, 0)]
    assert [k for k, _ in events] == ['message.accepted', 'approval.requested', 'approval.closed', 'run.cancelled']
    closed = events[2][1]
    assert closed['task_id'] == 't1'
    assert closed['request_ids'] == ['req-1']
    assert closed['reason'] == 'cancelled'
    assert isinstance(closed['request_seq'], int)


def test_workspace_select_emits_event(tmp_path):
    """切换工作空间写入 workspace.selected，含前值与空间类型。"""
    db = Store(tmp_path/'app.sqlite')
    sid = db.session()
    catalog = WorkspaceCatalog(db)
    first = catalog.current(sid)
    other = catalog.register('另一空间', str(tmp_path.parent/f'{tmp_path.name}-other'))
    catalog.select(sid, other['id'])
    row = db.query("SELECT * FROM events WHERE kind='workspace.selected'")[0]
    assert row['run'] is None
    assert json.loads(row['payload']) == {
        'previous_workspace_id': first['id'], 'workspace_id': other['id'], 'kind': 'device_fs',
    }


def test_task_resumed_after_approval(tmp_path):
    """审批后恢复执行的任务步骤发出 task.resumed，而不是重复 task.started。"""
    db, r = setup_task(tmp_path)
    async def planner(payload):
        return Plan(kind='tasks', items=[Item(key='a', goal='框架')])
    class Engine:
        async def execute(self, run):
            if not run.get('decision'):
                yield 'approval.requested', {'requests': []}
                return
            yield 'assistant.final', {'text': '完成'}
    runtime = TaskRuntime(tmp_path, db, Engine(), planner)
    async def collect(run):
        return [e async for e in runtime.execute(run)]
    first = asyncio.run(collect(r))
    assert [k for k, _ in first] == ['task.plan', 'task.started', 'approval.requested']
    db.finish(r, 'waiting'); db.decide(r['id'], 'approve')
    second = asyncio.run(collect(db.claim()))
    assert [k for k, _ in second] == ['task.resumed', 'task.delivered', 'assistant.final']
    assert second[0][1] == {'task_id': db.query('SELECT task FROM task_steps')[0]['task'], 'key': 'a', 'reason': 'approval'}


def test_task_revised_event(tmp_path):
    """持久化需求补丁生效后发出 task.revised，携带基础/生效版本。"""
    db, r = setup_task(tmp_path)
    repo = TaskRepository(db)
    repo.install(r, Plan(kind='tasks', items=[Item(key='a', goal='报告', constraints=['英语'])]), [])
    task = db.query('SELECT * FROM business_tasks')[0]
    db.finish(r, 'completed', '初稿')
    db.submit(r['session'], '改为中文', 'two'); r2 = db.claim()
    async def planner(payload):
        return Plan(kind='tasks', items=[Item(key='b', goal='更新报告', relation='refine', task_id=task['id'],
            task_patch=RequirementPatch(scope='persistent', expected_revision=0, operations=[
                RequirementOperation(op='replace', field='constraints', id='constraint_1', value='中文', evidence='改为中文')]))])
    class Engine:
        async def execute(self, run):
            yield 'assistant.final', {'text': '已更新'}
    runtime = TaskRuntime(tmp_path, db, Engine(), planner)
    async def collect():
        return [e async for e in runtime.execute(r2)]
    events = asyncio.run(collect())
    revised = next(p for k, p in events if k == 'task.revised')
    assert revised == {'task_id': task['id'], 'key': 'b', 'base_revision': 0, 'applied_revision': 1, 'scope': 'persistent'}
    assert [k for k, _ in events].index('task.plan') < [k for k, _ in events].index('task.revised')


def test_task_failed_and_interrupted_events(tmp_path):
    """步骤失败/中断分别发出 task.failed / task.interrupted 并保持步骤状态。"""
    db, r = setup_task(tmp_path)
    async def planner(payload):
        return Plan(kind='tasks', items=[Item(key='a', goal='框架'), Item(key='b', goal='说明')])
    state = {'n': 0}
    class Engine:
        async def execute(self, run):
            state['n'] += 1
            if state['n'] == 1:
                raise ValueError('模拟执行失败')
            raise asyncio.CancelledError()
            yield
    runtime = TaskRuntime(tmp_path, db, Engine(), planner)
    async def collect():
        events = []
        try:
            async for e in runtime.execute(r):
                events.append(e)
        except ValueError:
            return events
    events = asyncio.run(collect())
    step_a = db.query("SELECT task,status FROM task_steps WHERE key='a'")[0]
    assert events[-1] == ('task.failed', {'task_id': step_a['task'], 'key': 'a', 'reason': '模拟执行失败'})
    assert step_a['status'] == 'failed'
    async def collect_interrupted():
        events = []
        try:
            async for e in runtime.execute(r):
                events.append(e)
        except asyncio.CancelledError:
            return events
    events = asyncio.run(collect_interrupted())
    step_a = db.query("SELECT task,status FROM task_steps WHERE key='a'")[0]
    assert events[-1] == ('task.interrupted', {'task_id': step_a['task'], 'key': 'a', 'reason': 'cancelled'})
    assert step_a['status'] == 'interrupted'
