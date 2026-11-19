from artorias_device.storage.journal import SQLiteWorkspaceJournal
import asyncio
from pathlib import Path
import pytest
from artorias_server.storage.sqlite import Store
from artorias_server.workspace.catalog import WorkspaceCatalog
from artorias_local_app.bootstrap import create_workspace as WorkspaceService
from artorias_server.workspace.connectors import ConnectorUnavailable
from artorias_server.tasks.service import TaskRepository, Plan, Item


def setup(tmp_path,monkeypatch):
    monkeypatch.delenv('ARTORIAS_WORKSPACE',raising=False)
    data=tmp_path/'data';data.mkdir()
    store=Store(data/'app.sqlite');sid=store.session();cat=WorkspaceCatalog(store)
    local=cat.register('project',str(tmp_path/'project'))
    cloud=cat.register('cloud','personal','cloud_storage')
    cat.select(sid,local['id'])
    return store,sid,cat,local,cloud


def test_task_inherits_submission_snapshot_and_refine_stays(tmp_path,monkeypatch):
    db,sid,cat,local,cloud=setup(tmp_path,monkeypatch)
    db.submit(sid,'write project','1');run=db.claim()
    cat.select(sid,cloud['id'])
    repo=TaskRepository(db)
    repo.install(run,Plan(kind='tasks',items=[Item(key='a',goal='project')]),[])
    task=db.query('SELECT * FROM business_tasks')[0]
    assert cat.task_binding(task['id'],sid)['id']==local['id']
    ws=WorkspaceService(db,sid,run['id'],task['id'])
    ws.write('a.txt','local',expected=None)
    assert (tmp_path/'project/a.txt').read_text()=='local'
    db.finish(run,'completed')
    db.submit(sid,'refine and another task','2');r2=db.claim()
    repo.install(r2,Plan(kind='tasks',items=[Item(key='a',goal='refine',relation='refine',task_id=task['id']),Item(key='b',goal='new')]),repo.candidates(sid,'project'))
    tasks=db.query('SELECT * FROM business_tasks ORDER BY rowid')
    assert cat.task_binding(tasks[0]['id'],sid)['id']==local['id']
    assert cat.task_binding(tasks[1]['id'],sid)['id']==cloud['id']
    assert WorkspaceService(db,sid,run['id'],task['id']).describe()['id']==local['id']


def test_cloud_logical_write_read_backup_and_revert_after_switch(tmp_path,monkeypatch):
    db,sid,cat,local,cloud=setup(tmp_path,monkeypatch)
    cat.select(sid,cloud['id'])
    ws=WorkspaceService(db,sid,'run')
    change=ws.write('report.md','cloud report',expected=None)
    assert ws.read('report.md')=='cloud report'
    assert ws.files()==['report.md']
    rec=db.query('SELECT * FROM resource_changes')[0]
    assert rec['backend']=='cloud_storage'
    assert Path(rec['blob_root'])==tmp_path/'data/cloud/blobs'
    assert (Path(rec['blob_root'])/change['version']).exists()
    cat.select(sid,local['id'])
    WorkspaceService(db,sid).revert(change['change_id'])
    assert not (tmp_path/'data/cloud/files/personal/report.md').exists()


def test_external_grants_and_revocation_on_bound_run(tmp_path,monkeypatch):
    db,sid,cat,local,cloud=setup(tmp_path,monkeypatch)
    ws=WorkspaceService(db,sid,'run')
    outside=tmp_path/'downloads';outside.mkdir();target=outside/'x';target.write_text('original')
    with pytest.raises(PermissionError):ws.read(str(target))
    cat.grant(str(outside),'read','run')
    assert ws.read(str(target))=='original'
    with pytest.raises(PermissionError):ws.write(str(target),'bad')
    cat.grant(str(outside),'write','run')
    change=ws.write(str(target),'new',expected=ws.head(str(target)))
    assert db.query('SELECT blob_root FROM resource_changes')[0]['blob_root']==str(tmp_path/'data/blobs')
    with pytest.raises(PermissionError):WorkspaceService(db,sid,'other').read(str(target))
    with db.tx() as c:c.execute("DELETE FROM workspace_grants WHERE scope_run='run'")
    with pytest.raises(PermissionError):ws.revert(change['change_id'])
    assert target.read_text()=='new'


def test_uri_and_absolute_paths_do_not_change_default(tmp_path,monkeypatch):
    db,sid,cat,local,cloud=setup(tmp_path,monkeypatch)
    cat.select(sid,cloud['id']);ws=WorkspaceService(db,sid,'run')
    ws.write(str(tmp_path/'project/local.txt'),'local',expected=None)
    ws.write('cloud.txt','cloud',expected=None)
    assert ws.read('workspace://'+local['id']+'/local.txt')=='local'
    assert ws.describe()['id']==cloud['id']
    assert (tmp_path/'project/local.txt').exists()
    assert (tmp_path/'data/cloud/files/personal/cloud.txt').exists()
    for path in ['../oops','workspace://'+cloud['id']+'/../oops']:
        with pytest.raises(ValueError):ws.write(path,'bad')


def test_connector_unavailable_never_falls_back(tmp_path,monkeypatch):
    db,sid,cat,local,cloud=setup(tmp_path,monkeypatch)
    ws=WorkspaceService(db,sid,'run');ws.registry.adapters.clear()
    with pytest.raises(ConnectorUnavailable):ws.write('a','x')
    assert not (tmp_path/'project/a').exists()


def test_migration_preserves_old_task_and_change(tmp_path,monkeypatch):
    from artorias_device.workspace.local_workspace import LocalWorkspace
    db,sid,cat,local,cloud=setup(tmp_path,monkeypatch)
    old=LocalWorkspace(tmp_path/'data/blobs',SQLiteWorkspaceJournal(db.path),sid,'old',workspace_root=tmp_path/'project')
    change=old.write('old.txt','old')
    repo=TaskRepository(db)
    with db.tx() as c:
        c.execute("INSERT INTO business_tasks(id,session,source_task,goal,constraints,status,summary,updated) VALUES('legacy',?,NULL,'old','[]','open','',0)",(sid,))
        c.execute('DELETE FROM session_workspaces WHERE session=?',(sid,))
    assert cat.task_binding('legacy',sid)['root']==str(tmp_path/'project')
    cat.select(sid,cloud['id'])
    WorkspaceService(db,sid).revert(change['change_id'])
    assert not (tmp_path/'project/old.txt').exists()


def test_web_selection_grants_and_current_space(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from artorias_local_app.bootstrap import create_app
    class Engine:
        def __init__(self,*args):pass
        async def execute(self,run):yield 'assistant.final',{'text':'done'}
    monkeypatch.delenv('ARTORIAS_WORKSPACE',raising=False)
    with TestClient(create_app(tmp_path/'data',Engine)) as client:
        h={'x-artorias-token':client.get('/bootstrap').json()['token']}
        sid=client.post('/api/sessions',headers=h).json()['id']
        cloud=client.post('/api/workspaces',headers=h,json={'name':'云端','root':'reports','kind':'cloud_storage'}).json()
        assert client.post('/api/sessions/'+sid+'/workspace',headers=h,json={'workspace_id':cloud['id']}).status_code==200
        assert client.get('/api/sessions/'+sid+'/workspace',headers=h).json()['kind']=='cloud_storage'
        assert client.get('/api/sessions/'+sid+'/file',headers=h,params={'path':str(tmp_path/'private')}).status_code==403
        grants=client.get('/api/workspace-grants',headers=h).json()
        gid=next(g['id'] for g in grants if g['root']=='reports')
        assert client.post('/api/workspace-grants/'+gid+'/revoke',headers=h,json={}).status_code==200
        assert client.get('/api/sessions/'+sid+'/files',headers=h).status_code==403


def test_real_agent_authorization_resume(tmp_path,monkeypatch):
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage
    import artorias_server.runtime.service as runtime
    db,sid,cat,local,cloud=setup(tmp_path,monkeypatch)
    outside=tmp_path/'external'
    class Model(GenericFakeChatModel):
        def bind_tools(self,tools,**kwargs):return self
    def call(name,args,cid):return AIMessage(content='',tool_calls=[{'name':name,'args':args,'id':cid,'type':'tool_call'}])
    model=Model(disable_streaming=True,messages=iter([
        call('project_authorize',{'path':str(outside),'access':'write'},'grant'),
        call('project_write',{'path':str(outside/'a.txt'),'content':'approved'},'write'),AIMessage(content='done')]))
    monkeypatch.setattr(runtime,'create_model',lambda:model)
    db.submit(sid,'create external file','1');run=db.claim()
    from artorias_local_app.bootstrap import create_engine
    engine=create_engine(tmp_path/'data',db)
    async def collect(r):return [e async for e in engine.execute(r)]
    first=asyncio.run(collect(run));assert first[-1][0]=='approval.requested'
    assert not outside.exists()
    cat.select(sid,cloud['id'])
    second=asyncio.run(collect(dict(run,decision='approve')))
    assert second[-1][0]=='assistant.final'
    assert (outside/'a.txt').read_text()=='approved'
    assert WorkspaceService(db,sid,run['id']).describe()['id']==local['id']
