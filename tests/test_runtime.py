from task_fixtures import plan_item, finish_message, deliver
from artorias_device.storage.journal import SQLiteWorkspaceJournal
import asyncio
import json
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from artorias_server.storage.sqlite import Store
from artorias_local_app.bootstrap import create_engine as DeepAgentRuntime
from artorias_device.workspace.local_workspace import LocalWorkspace

class FakeModel(GenericFakeChatModel):
    def bind_tools(self,tools,**kwargs):return self

class StreamingToolModel(FakeModel):
    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        response = self._generate(messages, stop, run_manager, **kwargs).generations[0].message
        if not response.tool_calls:
            yield ChatGenerationChunk(message=AIMessageChunk(content=response.content))
            return
        call = response.tool_calls[0]
        arguments = json.dumps(call['args'])
        for chunk in (
            {'name':call['name'], 'id':call['id'], 'args':'', 'index':0},
            {'name':None, 'id':None, 'args':arguments[:1], 'index':0},
            {'name':None, 'id':None, 'args':arguments[1:], 'index':0},
        ):
            yield ChatGenerationChunk(message=AIMessageChunk(content='',tool_call_chunks=[chunk]))

def test_real_deepagents_tool_loop(tmp_path,monkeypatch):
    import artorias_server.runtime.service as rt
    model=StreamingToolModel(messages=iter([AIMessage(content='',tool_calls=[{'name':'project_write','args':{'path':'hello.py','content':'print(42)'},'id':'call_1','type':'tool_call'}]),AIMessage(content='文件已生成')]))
    monkeypatch.setenv('ARTORIAS_MODEL','fake');monkeypatch.setattr(rt,'create_model',lambda:model)
    db=Store(tmp_path/'app.sqlite');sid=db.session();db.submit(sid,'写代码','k');run=db.claim()
    async def execute():return [e async for e in DeepAgentRuntime(tmp_path,db).execute(run)]
    events=asyncio.run(execute())
    assert events[-1]==('assistant.final',{'text':'文件已生成'})
    tool_events=[(kind,payload['name'],payload['call_id']) for kind,payload in events if kind.startswith('tool.')]
    assert tool_events==[('tool.call','project_write','call_1'),('tool.result','project_write','call_1')]
    assert next(payload['args'] for kind,payload in events if kind=='tool.call')=={'path':'hello.py','content':'print(42)'}
    assert LocalWorkspace(tmp_path/'blobs',SQLiteWorkspaceJournal(db.path),sid,run['id']).read('hello.py')=='print(42)'

def test_web_job_completion(tmp_path):
    import time
    from fastapi.testclient import TestClient
    from artorias_local_app.main import create_app
    class FakeRuntime:
        def __init__(self,*args):pass
        async def execute(self,run):yield 'assistant.final',{'text':'hello'}
    with TestClient(create_app(tmp_path,FakeRuntime)) as client:
        assert client.get('/api/sessions').status_code==401
        headers={'x-artorias-token':client.get('/bootstrap').json()['token']}
        sid=client.post('/api/sessions',headers=headers).json()['id']
        response=client.post(f'/api/sessions/{sid}/messages',headers=headers,json={'text':'hi','request_key':'one'})
        assert response.status_code==200
        db=Store(tmp_path/'app.sqlite')
        for _ in range(50):
            if db.query('SELECT status FROM runs')[0]['status']=='completed' and db.events(sid,0)[-1]['kind']=='run.completed':break
            time.sleep(.02)
        assert db.query('SELECT status FROM runs')[0]['status']=='completed'
        assert db.events(sid,0)[-1]['kind']=='run.completed'

def test_followup_saves_previous_delivery_without_reopening_files(tmp_path, monkeypatch):
    from artorias_local_app.bootstrap import create_workspace
    from artorias_server.scheduler.service import Scheduler
    from artorias_server.tasks.executor import TaskRuntime
    from artorias_server.tasks.models import Item, Plan
    from artorias_server.tasks.repository import TaskRepository

    monkeypatch.setenv('ARTORIAS_WORKSPACE','')
    store=Store(tmp_path/'app.sqlite')
    sid=store.session()
    store.submit(sid,'扫描原目录','scan')
    scan=store.claim()
    repo=TaskRepository(store)
    repo.install(scan,Plan(kind='tasks',items=[plan_item(key='scan',goal='扫描原目录')]),[])
    source=store.query('SELECT * FROM task_steps')[0]
    summary='原始报告：只保留这份已经交付的摘要。'
    deliver(repo,scan,source,summary)
    store.finish(scan,'completed',summary)
    store.submit(sid,'把上面的摘要保存到原目录','save')
    save=store.claim()

    class FollowupModel(FakeModel):
        def _generate(self, messages, *args, **kwargs):
            assert any(summary in str(message.content) and 'source_results' in str(message.content) for message in messages)
            return super()._generate(messages,*args,**kwargs)

    model=FollowupModel(disable_streaming=True,messages=iter([
        AIMessage(content='',tool_calls=[{'name':'project_write','args':{'path':'summary.md','content':summary},'id':'write'}]),
        finish_message('已保存摘要'),
    ]))
    async def planner(_):
        return Plan(kind='tasks',items=[plan_item(key='save',goal='保存已有摘要',relation='followup',task_id=source['task'])])

    runtime=TaskRuntime(tmp_path,store,engine=DeepAgentRuntime(tmp_path,store,model_factory=lambda:model),planner=planner)
    asyncio.run(Scheduler(store,runtime).execute(save))
    calls=[json.loads(event['payload'])['name'] for event in store.events(sid,0) if event['kind']=='tool.call']
    assert calls==['project_write','finish_step']
    assert create_workspace(store,sid,save['id']).read('summary.md')==summary


def test_approval_reject_resume(tmp_path,monkeypatch):
    import artorias_server.runtime.service as rt
    replies=iter([AIMessage(content='',tool_calls=[{'name':'run_python','args':{'path':'x.py'},'id':'code1','type':'tool_call'}]),AIMessage(content='已取消运行')])
    model=FakeModel(disable_streaming=True,messages=replies)
    monkeypatch.setenv('ARTORIAS_MODEL','fake');monkeypatch.setattr(rt,'create_model',lambda:model)
    db=Store(tmp_path/'app.sqlite');sid=db.session();db.submit(sid,'运行','k');run=db.claim()
    from artorias_local_app.bootstrap import create_workspace
    from artorias_device.tools.python_backend import DockerPythonBackend
    create_workspace(db,sid,run['id']).write('x.py','print(42)',expected=None)
    monkeypatch.setattr(DockerPythonBackend,'profile',lambda self:{'image':'test-image','location':'device'})
    async def collect(r):return [e async for e in DeepAgentRuntime(tmp_path,db).execute(r)]
    first=asyncio.run(collect(run));assert first[-1][0]=='approval.requested'
    request=first[-1][1]['requests'][0]
    assert request['request_id'] and request['key']=='code_execution'
    db.finish(run,'waiting',approval={**first[-1][1],'task_id':'task-123'})
    seq=db.events(sid,0)[-1]['seq']
    with __import__('pytest').raises(ValueError):db.decide(run['id'],'reject',seq-1)
    db.decide(run['id'],'reject',seq)
    decision=db.events(sid,0)[-1]
    assert decision['kind']=='approval.decided'
    assert json.loads(decision['payload'])=={
        'decision':'reject','task_id':'task-123','request_seq':seq,
        'requests':[{'request_id':request['request_id'],'key':'code_execution','kind':'code_execution'}],
    }
    with __import__('pytest').raises(ValueError):db.decide(run['id'],'approve')
    resumed=db.claim();assert resumed['id']==run['id']
    second=asyncio.run(collect(resumed));assert second[-1][0]=='assistant.final'
    assert not any(kind=='tool.call' for kind,_ in second)
