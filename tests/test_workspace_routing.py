from task_fixtures import plan_item, finish_message
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
    repo.install(run,Plan(kind='tasks',items=[plan_item(key='a',goal='project')]),[])
    task=db.query('SELECT * FROM business_tasks')[0]
    assert cat.task_binding(task['id'],sid)['id']==local['id']
    ws=WorkspaceService(db,sid,run['id'],task['id'])
    ws.write('a.txt','local',expected=None)
    assert (tmp_path/'project/a.txt').read_text()=='local'
    db.finish(run,'completed')
    db.submit(sid,'refine and another task','2');r2=db.claim()
    repo.install(r2,Plan(kind='tasks',items=[plan_item(key='a',goal='refine',relation='refine',task_id=task['id']),plan_item(key='b',goal='new')]),repo.candidates(sid,'project'))
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


def test_project_move_tool_across_local_workspaces_and_undo(tmp_path, monkeypatch):
    from artorias_server.tools.registry import build_tools

    db, sid, cat, local, cloud = setup(tmp_path, monkeypatch)
    archive = cat.register('archive', str(tmp_path / 'archive'))
    archived = tmp_path / 'archive'
    archived.mkdir()
    ws = WorkspaceService(db, sid, 'run')
    ws.write('original.md', 'content', expected=None)
    move = next(tool for tool in build_tools(ws, lambda *_: None) if tool.name == 'project_move')
    source = tmp_path / 'project' / 'original.md'
    destination = archived / 'renamed.md'
    target = f"workspace://{archive['id']}/renamed.md"
    original_inode = source.stat().st_ino
    destination.write_text('existing')
    with pytest.raises(FileExistsError):
        move.invoke({'source': 'original.md', 'destination': target})
    assert source.read_text() == 'content' and destination.read_text() == 'existing'
    destination.unlink()
    with pytest.raises(ValueError, match='版本冲突'):
        move.invoke({'source': 'original.md', 'destination': target, 'expected_source_version': 'stale'})
    with pytest.raises(PermissionError):
        move.invoke({'source': 'original.md', 'destination': str(tmp_path / 'unauthorized.md')})
    with pytest.raises(ConnectorUnavailable):
        move.invoke({'source': 'original.md', 'destination': f"workspace://{cloud['id']}/original.md"})

    result = move.invoke({'source': 'original.md', 'destination': target,
                          'expected_source_version': ws.head('original.md')})
    assert not source.exists() and destination.stat().st_ino == original_inode
    change = next(row for row in ws.registry.list_changes(sid) if row['id'] == result['change_id'])
    assert change['source']['workspace_id'] == local['id']
    assert change['destination']['workspace_id'] == archive['id']
    assert change['kind'] == 'file.moved'
    cat.select(sid, archive['id'])
    WorkspaceService(db, sid).revert(result['change_id'])
    assert source.read_text() == 'content' and not destination.exists()


def test_project_move_cross_volume_copy_and_interrupted_source_removal(tmp_path, monkeypatch):
    from artorias_device.workspace import moves

    db, sid, cat, _, _ = setup(tmp_path, monkeypatch)
    archive = cat.register('archive', str(tmp_path / 'archive'))
    destination = tmp_path / 'archive' / 'result.bin'
    destination.parent.mkdir()
    ws = WorkspaceService(db, sid, 'run')
    source = tmp_path / 'project' / 'original.bin'
    source.parent.mkdir(exist_ok=True)
    data = b'\x00\xff' * 500_001
    source.write_bytes(data)
    source.chmod(0o640)
    original_identity = moves._identity

    def other_volume(path):
        identity = original_identity(path)
        if identity is not None and path == source:
            identity[0] = -1
        return identity

    monkeypatch.setattr(moves, '_identity', other_volume)
    target = f"workspace://{archive['id']}/result.bin"
    original_unlink = Path.unlink

    def fail_source_removal(path, *args, **kwargs):
        if path == source:
            raise OSError('simulated interruption')
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as failure:
        failure.setattr(Path, 'unlink', fail_source_removal)
        with pytest.raises(OSError, match='simulated interruption'):
            ws.move('original.bin', target)
    assert source.read_bytes() == destination.read_bytes() == data
    assert destination.stat().st_mode & 0o777 == 0o640
    assert db.query("SELECT status FROM workspace_operations WHERE kind='file.moved'") == [{'status': 'pending'}]
    with pytest.raises(ValueError, match='未解决'):
        ws.delete(target)

    # Once the duplicate is removed, recovery sees the original state and
    # clears the pending move. The next attempt can finish without data loss.
    destination.unlink()
    result = ws.move('original.bin', target)
    assert result['change_id']
    assert not source.exists() and destination.read_bytes() == data


def test_project_case_only_rename_recovers_journal_after_disk_change(tmp_path, monkeypatch):
    db, sid, _, _, _ = setup(tmp_path, monkeypatch)
    ws = WorkspaceService(db, sid, 'run')
    ws.write('Report.md', 'body', expected=None)
    journal = ws.registry.changes

    def interrupt(_):
        raise OSError('simulated failure after rename')

    with monkeypatch.context() as failure:
        failure.setattr(journal, 'finish', interrupt)
        with pytest.raises(OSError, match='simulated failure'):
            ws.move('Report.md', 'report.md')
    assert [entry.name for entry in (tmp_path / 'project').iterdir()] == ['report.md']
    assert ws.read('report.md') == 'body'
    change = next(row for row in journal.list_changes(sid) if row.get('kind') == 'file.moved')
    ws.revert(change['id'])
    assert [entry.name for entry in (tmp_path / 'project').iterdir()] == ['Report.md']


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


@pytest.mark.parametrize('decision', ['approve', 'remember', 'reject'])
def test_real_agent_authorization_resume(tmp_path,monkeypatch,decision):
    import json
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage
    from artorias_server.scheduler.service import Scheduler
    from artorias_server.tasks.service import TaskRuntime
    import artorias_server.runtime.service as runtime
    db,sid,cat,local,cloud=setup(tmp_path,monkeypatch)
    outside=tmp_path/'external'
    (outside/'nested').mkdir(parents=True)
    (outside/'nested/existing.txt').write_text('outside')
    class Model(GenericFakeChatModel):
        def bind_tools(self,tools,**kwargs):
            by_name={tool.name:tool for tool in tools}
            assert {'project_tree','project_list','project_read','project_authorize'} <= by_name.keys()
            assert not {'ls','glob','grep','write_file','edit_file','delete','execute'} & by_name.keys()
            assert 'cannot access the Mac' in by_name['read_file'].description
            return self
    def call(name,args,cid):return AIMessage(content='',tool_calls=[{'name':name,'args':args,'id':cid,'type':'tool_call'}])
    replies=[
        call('project_list',{'path':str(outside)},'list-before'),
        finish_message('done'),
    ]
    model=Model(disable_streaming=True,messages=iter(replies))
    monkeypatch.setattr(runtime,'create_model',lambda:model)
    db.submit(sid,'create external file','1');run=db.claim()
    from artorias_local_app.bootstrap import create_engine
    async def planner(_):return Plan(kind='tasks',items=[plan_item(key='write',goal='create external file')])
    runtime=TaskRuntime(tmp_path/'data',db,engine=create_engine(tmp_path/'data',db),planner=planner)
    scheduler=Scheduler(db,runtime)
    asyncio.run(scheduler.execute(run))
    request=db.events(sid,0)[-1]
    assert request['kind']=='approval.requested'
    assert db.query('SELECT status FROM task_steps WHERE run=?',(run['id'],))[0]['status']=='waiting'
    assert not db.query('SELECT id FROM workspace_grants WHERE scope_run=?',(run['id'],))
    cat.select(sid,cloud['id'])
    request_item = json.loads(request['payload'])['requests'][0]
    db.decide(run['id'],{request_item['request_id']: decision},request['seq'])
    asyncio.run(scheduler.execute(db.claim()))
    events=[(row['kind'],json.loads(row['payload'])) for row in db.events(sid,0)]
    assert sum(kind=='task.resumed' for kind,_ in events)==1
    assert events[-1][0]=='run.completed'
    listing=next(payload for kind,payload in events if kind=='tool.result' and payload['name']=='project_list')
    if decision=='reject':
        assert json.loads(listing['content'])=={'rejected':True}
    else:
        assert json.loads(listing['content'])['entries']==[{'path':'nested','type':'directory'}]
    next_ws = WorkspaceService(db, sid, 'another-run')
    if decision == 'remember':
        assert next_ws.read(str(outside/'nested/existing.txt')) == 'outside'
        with pytest.raises(PermissionError):
            WorkspaceService(db, db.session(), 'another-session').read(str(outside/'nested/existing.txt'))
    else:
        with pytest.raises(PermissionError):
            next_ws.read(str(outside/'nested/existing.txt'))
    assert WorkspaceService(db,sid,run['id']).describe()['id']==local['id']


def test_project_list_directory_targets(tmp_path,monkeypatch):
    from artorias_server.tools.registry import build_tools

    db,sid,_,local,_=setup(tmp_path,monkeypatch)
    ws=WorkspaceService(db,sid,'listing-run')
    project=Path(local['root'])
    (project/'nested/empty').mkdir(parents=True)
    (project/'nested/a.txt').write_text('hello')
    (project/'linked').symlink_to(project/'nested',target_is_directory=True)
    tool=next(tool for tool in build_tools(ws,lambda *_:None) if tool.name=='project_list')
    nested=[{'path':'a.txt','type':'file'},{'path':'empty','type':'directory'}]
    assert tool.invoke({})['entries']==[{'path':'nested','type':'directory'}]
    assert tool.invoke({'path':'nested'})['entries']==nested
    assert tool.invoke({'path':f"workspace://{local['id']}/nested"})['entries']==nested
    assert tool.invoke({'path':str(project/'nested/empty')})['entries']==[]
    with pytest.raises(NotADirectoryError):
        tool.invoke({'path':'nested/a.txt'})
    with pytest.raises(ValueError,match='符号链接'):
        tool.invoke({'path':'linked'})
    missing=tmp_path/'missing'
    ws.grant_access(str(missing),'read')
    with pytest.raises(FileNotFoundError):
        tool.invoke({'path':str(missing)})
    assert not missing.exists()


def test_project_read_paginates_and_rejects_changed_versions(tmp_path, monkeypatch):
    import json
    from artorias_server.tools.registry import build_tools

    db,sid,_,local,_=setup(tmp_path,monkeypatch)
    ws=WorkspaceService(db,sid,'paging-run')
    root=Path(local['root'])
    root.mkdir(exist_ok=True)
    contents='字符🙂\n' * 1300
    (root/'a.txt').write_text(contents)
    (root/'b.txt').touch()
    tools={tool.name:tool for tool in build_tools(ws,lambda *_:None)}
    read=tools['project_read'].invoke
    page=read({'path':'a.txt','limit':4000})
    assert page['has_more'] and page['content']
    assert len(json.dumps(page['content'],ensure_ascii=False).encode('utf-8')) <= 8192
    combined=page['content']
    while page['has_more']:
        page=read({'path':'a.txt','limit':4000,'offset':page['next_offset'],'expected_version':page['version']})
        combined+=page['content']
    assert combined==contents
    (root/'a.txt').write_text('changed')
    with pytest.raises(ValueError,match='内容已变更'):
        read({'path':'a.txt','offset':2,'expected_version':page['version']})


def test_project_tree_and_unbounded_directory_list(tmp_path, monkeypatch):
    from pydantic import ValidationError
    from artorias_server.tools.registry import build_tools

    db,sid,cat,local,cloud=setup(tmp_path,monkeypatch)
    local_root=Path(local['root'])
    (local_root/'nested/deeper').mkdir(parents=True)
    (local_root/'nested/deeper/file.txt').touch()
    (local_root/'.hidden').touch()
    (local_root/'link').symlink_to(local_root/'nested',target_is_directory=True)
    ws=WorkspaceService(db,sid)
    tools={tool.name:tool for tool in build_tools(ws,lambda *_:None)}
    tree=tools['project_tree'].invoke
    assert tree({})=={'path':'.','entries':[
        {'path':'nested','type':'directory'}, {'path':'nested/deeper','type':'directory'},
    ],'truncated':False}
    assert tree({'max_entries':2})['truncated'] is False
    assert tree({'max_entries':1})['truncated'] is True
    assert tree({'max_depth':1,'include_hidden':True})['entries']==[
        {'path':'.hidden','type':'file'}, {'path':'nested','type':'directory'},
    ]
    with pytest.raises(ValidationError):
        tree({'max_entries':1001})
    with pytest.raises(PermissionError):
        tree({'path':str(tmp_path/'outside')})

    cat.select(sid,cloud['id'])
    cloud_ws=WorkspaceService(db,sid)
    tools={tool.name:tool for tool in build_tools(cloud_ws,lambda *_:None)}
    tools['project_write'].invoke({'path':'remote/hello.txt','content':'first'})
    tools['project_edit'].invoke({'path':'remote/hello.txt','old':'first','new':'cloud'})
    assert tools['project_read'].invoke({'path':'remote/hello.txt'})['content']=='cloud'
    assert tools['project_tree'].invoke({})['entries']==[
        {'path':'remote','type':'directory'}, {'path':'remote/hello.txt','type':'file'},
    ]
    for index in range(1001):
        (tmp_path/'data/cloud/files/personal'/f'file-{index}').touch()
    listing=tools['project_list'].invoke({})
    assert len(listing['entries'])==1002
    assert 'truncated' not in listing
    tree_result=tools['project_tree'].invoke({'max_entries':1000})
    assert len(tree_result['entries'])==1000 and tree_result['truncated'] is True
