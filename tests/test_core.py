from artorias_device.storage.journal import SQLiteWorkspaceJournal
import pytest
from artorias_server.storage.sqlite import Store
from artorias_device.workspace.local_workspace import LocalWorkspace

def setup(tmp_path):
    db=Store(tmp_path/'app.db');s=db.session();return db,s,LocalWorkspace(tmp_path/'blobs',SQLiteWorkspaceJournal(db.path),s,'r')
def test_dedup(tmp_path):
    db,s,ws=setup(tmp_path)
    a=db.submit(s,'hi','k');assert db.submit(s,'hi','k')['id']==a['id']
    with pytest.raises(ValueError):db.submit(s,'different','k')
    assert db.claim()['id']==a['id'];assert db.claim() is None
    db.recover();assert db.query('SELECT status FROM runs')[0]['status']=='interrupted'
def test_versions(tmp_path):
    db,s,ws=setup(tmp_path);a=ws.write('a.py','one');b=ws.write('a.py','two')
    with pytest.raises(ValueError):ws.revert(a['change_id'])
    ws.revert(b['change_id']);assert ws.read('a.py')=='one'
    deleted=ws.delete('a.py');assert ws.files()==[]
    ws.revert(deleted);assert ws.read('a.py')=='one'
    assert db.events(s,0)
def test_paths(tmp_path):
    _,_,ws=setup(tmp_path)
    for p in ['../secret','/etc/passwd','a/../../bad']:
        with pytest.raises(ValueError):ws.write(p,'x')
def test_session_isolation(tmp_path):
    db,s,ws=setup(tmp_path);ws.write('x','private')
    other=LocalWorkspace(tmp_path/'blobs',SQLiteWorkspaceJournal(db.path),db.session(),'r2')
    assert other.files()==[]
    with pytest.raises(FileNotFoundError):other.read('x')
