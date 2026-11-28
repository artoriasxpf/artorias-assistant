from task_fixtures import plan_item, submission, finish_message
from artorias_device.storage.journal import SQLiteWorkspaceJournal
import asyncio
import json
import pytest
from artorias_server.storage.sqlite import Store
from artorias_server.tasks.service import TaskRepository, ContextBuilder, TaskRuntime, Plan, Item

def setup(tmp_path):
    db=Store(tmp_path/'app.sqlite');sid=db.session();db.submit(sid,'创建框架并写说明','one');return db,db.claim()

def test_validation_atomic(tmp_path):
    db,r=setup(tmp_path);repo=TaskRepository(db)
    for items in [[plan_item(key='a',goal='x',depends_on=['b'])], [plan_item(key='a',goal='x',relation='continue',task_id='foreign')]]:
        with pytest.raises(ValueError):repo.install(r,Plan(kind='tasks',items=items),[])
    assert not db.query('SELECT * FROM task_plans')

def test_resume_dependencies_and_original_input(tmp_path):
    db,r=setup(tmp_path);calls=[]
    async def planner(payload):return Plan(kind='tasks',items=[plan_item(key='b',goal='说明',depends_on=['a']),plan_item(key='a',goal='框架')])
    class Engine:
        async def execute(self, run):
            calls.append(run)
            if run['thread_id'].endswith(':b') and not run.get('decision'):
                yield 'approval.requested',{'requests':[]};return
            yield 'step.submitted', submission(run['task_context']['execution_scope']['goal']).model_dump()
            yield 'assistant.final', {'text':run['task_context']['execution_scope']['goal']}
    runtime=TaskRuntime(tmp_path,db,Engine(),planner)
    async def collect(run):return [e async for e in runtime.execute(run)]
    first=asyncio.run(collect(r));assert first[-1][0]=='approval.requested'
    assert calls[0]['input']==r['input']
    assert calls[1]['task_context']['dependency_results']==[{'key':'a','preview':'框架','result_ref':{'run_id':r['id'],'step_key':'a'}}]
    db.finish(r,'waiting');db.decide(r['id'],'approve')
    second=asyncio.run(collect(db.claim()))
    assert second[-1][1]['text']=='框架\n\n说明'
    assert len(calls)==3
    assert all(x['status']=='needs_review' for x in db.query('SELECT * FROM tasks'))
    assert len(db.query('SELECT * FROM context_snapshots'))==2

def test_refine_reuses_goal_and_context_budget(tmp_path):
    db,r=setup(tmp_path);repo=TaskRepository(db)
    repo.install(r,Plan(kind='tasks',items=[plan_item(key='a',goal='设计整个项目')]),[])
    task=db.query('SELECT * FROM tasks')[0]
    db.finish(r,'completed','框架已设计')
    db.submit(r['session'],'细化各模块','two');r2=db.claim()
    repo.install(r2,Plan(kind='tasks',items=[plan_item(key='b',goal='细化模块',relation='refine',task_id=task['id'])]),repo.candidates(r['session'],'细化各模块'))
    step=db.query('SELECT * FROM task_steps WHERE run=?',(r2['id'],))[0]
    body=ContextBuilder(repo).build(r2,step)
    assert body['task']['goal']=='设计整个项目'
    assert body['execution_scope']['goal']=='细化模块'
    assert body['history']
    assert len(db.query('SELECT * FROM tasks'))==1
    assert repo.candidates(db.session(),'项目')==[]

def test_two_tasks_with_actual_deepagents(tmp_path, monkeypatch):
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage
    import artorias_server.runtime.service as module
    from artorias_device.workspace.local_workspace import LocalWorkspace
    class Model(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):return self
    model=Model(disable_streaming=True,messages=iter([
        AIMessage(content='',tool_calls=[{'name':'project_write','args':{'path':'a.txt','content':'A'},'id':'a','type':'tool_call'}]),
        finish_message('A written'),
        AIMessage(content='',tool_calls=[{'name':'project_write','args':{'path':'b.txt','content':'B'},'id':'b','type':'tool_call'}]),
        finish_message('B written')]))
    monkeypatch.setattr(module,'create_model',lambda:model)
    db,r=setup(tmp_path)
    async def planner(payload):return Plan(kind='tasks',items=[plan_item(key='a',goal='写 A'),plan_item(key='b',goal='写 B',depends_on=['a'])])
    from artorias_local_app.bootstrap import create_engine
    runtime=TaskRuntime(tmp_path,db,engine=create_engine(tmp_path,db),planner=planner)
    async def collect():return [event async for event in runtime.execute(r)]
    events=asyncio.run(collect())
    assert events[-1][1]['text']=='A written\n\nB written'
    ws=LocalWorkspace(tmp_path/'blobs',SQLiteWorkspaceJournal(db.path),r['session'],r['id'])
    assert ws.read('a.txt')=='A' and ws.read('b.txt')=='B'

def test_essential_budget_fails_without_snapshot(tmp_path):
    db,r=setup(tmp_path);repo=TaskRepository(db)
    repo.install(r,Plan(kind='tasks',items=[plan_item(key='a',goal='goal')]),[])
    step=db.query('SELECT * FROM task_steps')[0]
    with pytest.raises(ValueError):ContextBuilder(repo,budget=10).build(r,step)
    assert not db.query('SELECT * FROM context_snapshots')


def test_planner_retries_null_patch_with_field_feedback(tmp_path, monkeypatch):
    from langchain_core.messages import AIMessage
    from artorias_server.tasks import planner as module

    db,run=setup(tmp_path)
    calls=[]
    replies=iter([
        '{"kind":"tasks","items":[{"key":"a","initial_requirements":{"goal":"example"},"step_spec":{"goal":"example"},"task_patch":null}]}',
        '{"kind":"tasks","items":[{"key":"a","initial_requirements":{"goal":"example"},"step_spec":{"goal":"example"},"task_patch":{"operations":[]}}]}',
    ])

    class Model:
        async def ainvoke(self, messages):
            calls.append(messages)
            return AIMessage(content=next(replies))

    monkeypatch.setattr(module,'create_model',Model)
    plan=asyncio.run(module.TaskPlanner(db).plan(run,[]))
    assert plan.items[0].task_patch.operations==[]
    assert len(calls)==2
    assert calls[0][1]==calls[1][1]
    assert 'items[0].task_patch' in calls[1][0]['content']
    assert [event['kind'] for event in db.events(run['session'],0)][-4:]==[
        'model.started','model.finished','model.started','model.finished',
    ]


def test_planner_stops_after_two_invalid_outputs(tmp_path, monkeypatch):
    from langchain_core.messages import AIMessage
    from artorias_server.scheduler.service import Scheduler
    from artorias_server.tasks import planner as module

    db,run=setup(tmp_path)
    calls=[]
    replies=iter([
        'not-json',
        '{"kind":"tasks","items":[{"key":"a","initial_requirements":{"goal":"example"},"step_spec":{"goal":"example"},'
        '"task_patch":{"expected_revision":"1","unexpected":true}}]}',
    ])

    class Model:
        async def ainvoke(self,messages):
            calls.append(messages)
            return AIMessage(content=next(replies))

    monkeypatch.setattr(module,'create_model',Model)
    asyncio.run(Scheduler(db,TaskRuntime(tmp_path,db,engine=object())).execute(run))
    assert len(calls)==2
    assert '必须返回有效的 JSON 对象' in calls[1][0]['content']
    assert not db.query('SELECT * FROM task_plans')
    assert not db.query('SELECT * FROM task_steps')
    event=db.events(run['session'],0)[-1]
    assert event['kind']=='run.failed'
    error=json.loads(event['payload'])['error']
    assert 'items[0].task_patch.expected_revision' in error
    assert 'items[0].task_patch.unexpected' in error
