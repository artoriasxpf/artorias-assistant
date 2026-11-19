from artorias_device.storage.journal import SQLiteWorkspaceJournal
import os
import pytest
from artorias_server.storage.sqlite import Store
from artorias_device.workspace.local_workspace import LocalWorkspace, digest
from artorias_server.tools.registry import build_tools
from artorias_device.workspace.export_legacy import export_legacy


def setup(tmp_path):
    data = tmp_path/'data'
    data.mkdir()
    db = Store(data/'app.sqlite')
    sid = db.session()
    ws = LocalWorkspace(data/'blobs',SQLiteWorkspaceJournal(db.path), sid, 'run', workspace_root=tmp_path/'project')
    return db, sid, ws


def test_existing_file_backup_real_write_and_revert(tmp_path):
    db,sid,ws = setup(tmp_path)
    p=ws.root/'existing.txt'
    p.write_text('user original')
    p.chmod(0o750)
    assert ws.read('existing.txt') == 'user original'
    assert ws.files() == ['existing.txt']
    change=ws.write('existing.txt','agent result')
    assert p.read_text() == 'agent result'
    assert p.stat().st_mode & 0o777 == 0o750
    assert (ws.blobs/digest(b'user original')).read_bytes()==b'user original'
    ws.revert(change['change_id'])
    assert p.read_text()=='user original'
    assert p.stat().st_mode & 0o777==0o750
    made=ws.write('nested/new.txt','new')
    assert (ws.root/'nested/new.txt').read_text()=='new'
    ws.revert(made['change_id'])
    assert not (ws.root/'nested/new.txt').exists()


def test_external_edit_delete_and_recreate_conflicts(tmp_path):
    _,_,ws=setup(tmp_path)
    a=ws.write('x','a')
    (ws.root/'x').write_text('editor')
    with pytest.raises(ValueError,match='版本冲突'):ws.revert(a['change_id'])
    with pytest.raises(ValueError,match='版本冲突'):ws.write('x','b',expected=a['version'])
    assert ws.read('x')=='editor'
    deleted=ws.delete('x')
    (ws.root/'x').write_text('new user file')
    with pytest.raises(ValueError,match='版本冲突'):ws.revert(deleted)
    (ws.root/'x').unlink()
    ws.revert(deleted)
    assert ws.read('x')=='editor'


def test_binding_shared_workspace_and_restart(tmp_path,monkeypatch):
    db,sid,ws=setup(tmp_path)
    ws.write('x','shared')
    monkeypatch.setenv('ARTORIAS_WORKSPACE',str(tmp_path/'different'))
    again=LocalWorkspace(ws.blobs,SQLiteWorkspaceJournal(db.path),sid,'r2')
    assert again.root==ws.root
    other=LocalWorkspace(ws.blobs,SQLiteWorkspaceJournal(db.path),db.session(),'r3',workspace_root=ws.root)
    assert other.read('x')=='shared'
    other.write('x','second session')
    assert ws.read('x')=='second session'


def test_symlinks_absolute_and_metadata_boundaries(tmp_path):
    db,sid,ws=setup(tmp_path)
    outside=tmp_path/'outside';outside.mkdir()
    (outside/'secret').write_text('secret')
    (ws.root/'link').symlink_to(outside,target_is_directory=True)
    for action in [lambda:ws.read('link/secret'),lambda:ws.write('link/new','x'),lambda:ws.delete('link/secret')]:
        with pytest.raises(ValueError):action()
    assert ws.files()==[]
    ws.write(str(ws.root/'ok'),'allowed')
    with pytest.raises(ValueError):ws.write(str(outside/'secret'),'bad')
    with pytest.raises(ValueError):LocalWorkspace(ws.blobs,SQLiteWorkspaceJournal(db.path),db.session(),'r',workspace_root=tmp_path)
    assert (outside/'secret').read_text()=='secret'


def test_crash_after_replace_reconciles_once(tmp_path,monkeypatch):
    db,sid,ws=setup(tmp_path)
    def crash(op):raise RuntimeError('simulated database failure')
    monkeypatch.setattr(ws,'_finish',crash)
    with pytest.raises(RuntimeError):ws.write('x','persisted')
    assert (ws.root/'x').read_text()=='persisted'
    assert db.query('SELECT * FROM changes')==[]
    again=LocalWorkspace(ws.blobs,SQLiteWorkspaceJournal(db.path),sid,'recover')
    assert len(db.query('SELECT * FROM changes'))==1
    assert len(db.query("SELECT * FROM events WHERE kind='file.changed'"))==1
    LocalWorkspace(ws.blobs,SQLiteWorkspaceJournal(db.path),sid,'recover2')
    assert len(db.query('SELECT * FROM changes'))==1
    again.revert(db.query('SELECT id FROM changes')[0]['id'])
    assert not (ws.root/'x').exists()


def test_crash_before_replace_and_ambiguous_state(tmp_path,monkeypatch):
    db,sid,ws=setup(tmp_path)
    ws.write('x','before')
    original=ws._atomic
    def fail(target,data,mode):
        if target==ws.root/'x':raise OSError('disk failure')
        return original(target,data,mode)
    monkeypatch.setattr(ws,'_atomic',fail)
    with pytest.raises(OSError):ws.write('x','after')
    LocalWorkspace(ws.blobs,SQLiteWorkspaceJournal(db.path),sid,'recover')
    assert db.query("SELECT * FROM workspace_operations WHERE status='aborted'")
    with pytest.raises(OSError):ws.write('x','after')
    (ws.root/'x').write_text('external after crash')
    recovered=LocalWorkspace(ws.blobs,SQLiteWorkspaceJournal(db.path),sid,'recover')
    with pytest.raises(ValueError,match='未解决'):recovered.write('x','overwrite')
    assert recovered.read('x')=='external after crash'
    recovered.write('unrelated','works')


def test_tool_observed_version_and_binary_delete(tmp_path):
    _,_,ws=setup(tmp_path)
    ws.write('x','original')
    tools={t.name:t for t in build_tools(ws,lambda *a:None)}
    with pytest.raises(ValueError,match='project_read'):tools['project_write'].invoke({'path':'x','content':'bad'})
    tools['project_read'].invoke({'path':'x'})
    (ws.root/'x').write_text('editor')
    with pytest.raises(ValueError,match='版本冲突'):tools['project_write'].invoke({'path':'x','content':'bad'})
    (ws.root/'binary').write_bytes(b'\x00\xff')
    cid=ws.delete('binary');ws.revert(cid)
    assert (ws.root/'binary').read_bytes()==b'\x00\xff'


def test_legacy_export_never_overwrites(tmp_path):
    db,sid,ws=setup(tmp_path)
    ws.write('a.txt','backup')
    dest=tmp_path/'export'
    assert export_legacy(ws.blobs.parent,sid,dest)==1
    assert (dest/'a.txt').read_text()=='backup'
    with pytest.raises(FileExistsError):export_legacy(ws.blobs.parent,sid,dest)


def test_web_reads_bound_tree_and_reverts_disk(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from artorias_local_app.bootstrap import create_app
    project=tmp_path/'project';project.mkdir()
    (project/'preexisting.txt').write_text('outside agent')
    (project/'large.bin').write_bytes(b'x'*1_000_001)
    monkeypatch.setenv('ARTORIAS_WORKSPACE',str(project))
    class FakeRuntime:
        def __init__(self,*args):pass
        async def execute(self,run):yield 'assistant.final',{'text':'done'}
    data=tmp_path/'data'
    with TestClient(create_app(data,FakeRuntime)) as client:
        headers={'x-artorias-token':client.get('/bootstrap').json()['token']}
        sid=client.post('/api/sessions',headers=headers).json()['id']
        url=f'/api/sessions/{sid}'
        assert client.get(url+'/workspace',headers=headers).json()['root']==str(project)
        listing=client.get(url+'/files',headers=headers)
        assert listing.status_code==200
        assert {v['path'] for v in listing.json()}=={'preexisting.txt','large.bin'}
        assert client.get(url+'/file?path=preexisting.txt',headers=headers).json()['content']=='outside agent'
        assert client.get(url+'/file?path=missing',headers=headers).status_code==404
        db=Store(data/'app.sqlite');ws=LocalWorkspace(data/'blobs',SQLiteWorkspaceJournal(db.path),sid,'run')
        change=ws.write('preexisting.txt','agent')
        response=client.post(url+'/changes/'+change['change_id']+'/revert',headers=headers,json={})
        assert response.status_code==200
        assert (project/'preexisting.txt').read_text()=='outside agent'
