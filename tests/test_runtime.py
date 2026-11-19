from artorias_device.storage.journal import SQLiteWorkspaceJournal
import asyncio
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from artorias_server.storage.sqlite import Store
from artorias_local_app.bootstrap import create_engine as DeepAgentRuntime
from artorias_device.workspace.local_workspace import LocalWorkspace

class FakeModel(GenericFakeChatModel):
    def bind_tools(self,tools,**kwargs):return self

def test_real_deepagents_tool_loop(tmp_path,monkeypatch):
    import artorias_server.runtime.service as rt
    model=FakeModel(disable_streaming=True,messages=iter([AIMessage(content='',tool_calls=[{'name':'project_write','args':{'path':'hello.py','content':'print(42)'},'id':'call_1','type':'tool_call'}]),AIMessage(content='文件已生成')]))
    monkeypatch.setenv('ARTORIAS_MODEL','fake');monkeypatch.setattr(rt,'create_model',lambda:model)
    db=Store(tmp_path/'app.sqlite');sid=db.session();db.submit(sid,'写代码','k');run=db.claim()
    async def execute():return [e async for e in DeepAgentRuntime(tmp_path,db).execute(run)]
    events=asyncio.run(execute())
    assert events[-1]==('assistant.final',{'text':'文件已生成'})
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
    db.finish(run,'waiting');db.decide(run['id'],'reject')
    with __import__('pytest').raises(ValueError):db.decide(run['id'],'approve')
    resumed=db.claim();assert resumed['id']==run['id']
    second=asyncio.run(collect(resumed));assert second[-1][0]=='assistant.final'
