import asyncio
import sqlite3
import pytest
from artorias_server.storage.sqlite import Store
from artorias_server.tasks.repository import TaskRepository
from artorias_server.runtime.checkpoints import SQLiteCheckpointProvider
from artorias_server.runtime.budget import check_budget


def test_legacy_schema_and_all_timestamps(tmp_path):
    path=tmp_path/'app.db'
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE sessions(id TEXT PRIMARY KEY)')
        c.execute("INSERT INTO sessions VALUES('legacy')")
    db=Store(path)
    TaskRepository(db)
    first=db.query('SELECT * FROM sessions')[0]
    assert first['created_at'].endswith('Z') and first['updated_at']
    with db.tx() as c:
        c.execute("UPDATE sessions SET updated_at='2000-01-01T00:00:00Z' WHERE id='legacy'")
        c.execute("UPDATE sessions SET id='renamed' WHERE id='legacy'")
    second=db.query('SELECT * FROM sessions')[0]
    assert second['created_at']==first['created_at']
    assert second['updated_at']!='2000-01-01T00:00:00Z'
    for table in db.query("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        columns={r['name'] for r in db.query('PRAGMA table_info('+table['name']+')')}
        assert {'created_at','updated_at'} <= columns
    Store(path)
    assert db.query('SELECT * FROM sessions')[0]['created_at']==first['created_at']


def test_checkpointer_timestamps(tmp_path):
    path=tmp_path/'checkpoints.db'
    async def run():
        async with SQLiteCheckpointProvider(path).open(): pass
    asyncio.run(run())
    with sqlite3.connect(path) as c:
        for table in ('checkpoints','writes'):
            assert {'created_at','updated_at'} <= {r[1] for r in c.execute(f'PRAGMA table_info({table})')}


def test_budget_rejects_oversized_tools():
    with pytest.raises(ValueError): check_budget({'tools':[{'description':'大'*100}]},limit=100)
    assert check_budget({'input':'hello'},limit=100)<100


def test_web_modules_served(tmp_path):
    from fastapi.testclient import TestClient
    from artorias_server.api.app import create_http_app
    db=Store(tmp_path/'app.db')
    from artorias_local_app.bootstrap import create_app
    client=TestClient(create_app(tmp_path))
    page=client.get('/')
    assert page.status_code==200 and 'type="module"' in page.text
    assert '<style>' not in page.text and '<script>' not in page.text
    for file in ('app.js','api.js','events.js','workspace.js','styles.css'):
        assert client.get('/static/'+('css/' if file.endswith('.css') else 'js/')+file).status_code==200
    assert client.get('/api/sessions').status_code==401
