import asyncio
import json
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


def test_task_column_migration_keeps_stable_ids_counts_and_timestamps(tmp_path):
    from artorias_server.storage.migrations import ensure_timestamps
    from artorias_server.tasks.requirements import requirements_for

    path=tmp_path/'app.db'
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE business_tasks(id TEXT PRIMARY KEY, session TEXT NOT NULL, source_task TEXT,
                goal TEXT NOT NULL, constraints TEXT NOT NULL, status TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '', updated INTEGER NOT NULL DEFAULT 0,
                requirements TEXT NOT NULL DEFAULT '{}', execution_count INTEGER NOT NULL DEFAULT 0,
                revision INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX task_session ON business_tasks(session,updated);
        """)
        ensure_timestamps(conn)
        conn.execute("""
            INSERT INTO business_tasks(id,session,goal,constraints,status,summary,updated,requirements,execution_count,revision,created_at,updated_at)
            VALUES('versioned','s','old goal','["old rule"]','open','old summary',7,?,4,2,'2026-01-01T00:00:00.000Z','2026-03-01T00:00:00.000Z')
        """, (json.dumps({'goal':'current goal','constraints':{'rule-8':'use Chinese'},'criteria':{'criterion-3':'cite source'}}),))
        conn.execute("""
            INSERT INTO business_tasks(id,session,goal,constraints,status,updated,execution_count)
            VALUES('legacy','s','legacy goal','["English","sources"]','open',2,4)
        """)
        conn.execute("UPDATE business_tasks SET updated_at='2026-03-01T00:00:00.000Z' WHERE id='versioned'")

    db=Store(path)
    TaskRepository(db)
    tasks={row['id']:row for row in db.query('SELECT * FROM business_tasks')}
    assert {'requirements','updated'}.isdisjoint(tasks['versioned'])
    assert requirements_for(tasks['versioned'])=={
        'goal':'current goal','constraints':{'rule-8':'use Chinese'},'criteria':{'criterion-3':'cite source'},
    }
    assert requirements_for(tasks['legacy'])=={
        'goal':'legacy goal','constraints':{'constraint_1':'English','constraint_2':'sources'},'criteria':{},
    }
    assert tasks['versioned']['execution_count']==7 and tasks['legacy']['execution_count']==4
    assert tasks['versioned']['summary']=='old summary' and tasks['versioned']['revision']==2
    assert tasks['versioned']['updated_at']=='2026-03-01T00:00:00.000Z'
    TaskRepository(db)
    assert {row['id']:row for row in db.query('SELECT * FROM business_tasks')}==tasks
    with db.tx() as conn:
        conn.execute("UPDATE business_tasks SET goal='new' WHERE id='versioned'")
    assert db.query("SELECT updated_at FROM business_tasks WHERE id='versioned'")[0]['updated_at']!='2026-03-01T00:00:00.000Z'


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


def test_budget_preserves_call_pairs_and_paging_context():
    import json
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from artorias_server.runtime.budget import _fit_messages

    def call(name, call_id, args):
        return AIMessage(content='', tool_calls=[{'name': name, 'id': call_id, 'args': args}])

    def read_result(call_id, offset):
        return ToolMessage(name='project_read', tool_call_id=call_id, content=json.dumps({
            'path': 'notes.md', 'version': 'v1', 'offset': offset, 'next_offset': offset + 2000,
            'has_more': True, 'content': '长' * 1400,
        }, ensure_ascii=False))

    original = [HumanMessage(content='Summarize notes.md')]
    for index in range(49):
        call_id = f'read-{index}'
        original += [call('project_read', call_id, {'path': 'notes.md', 'offset': index * 2000}),
                     read_result(call_id, index * 2000)]
    original += [call('project_write', 'write', {'path': 'report.md', 'content': '字' * 15000}),
                 ToolMessage(name='project_write', tool_call_id='write', content='saved')]
    latest = read_result('latest', 98000)
    pending = call('project_authorize', 'pending', {'path': '/other', 'access': 'read'})
    original += [call('project_read', 'latest', {'path': 'notes.md'}), latest, pending]
    with pytest.raises(ValueError):
        check_budget({'messages': [message.model_dump() for message in original]}, 160000)

    fitted = _fit_messages(original, {}, 160000)
    check_budget({'messages': [message.model_dump() for message in fitted]}, 160000)
    assert fitted[0] is original[0] and fitted[-1] is pending and fitted[-2] is latest
    assert '长' * 1400 in original[2].content
    assert any('next_offset' in str(message.content) for message in fitted[1:-3])
    for index, message in enumerate(fitted[:-1]):
        if isinstance(message, AIMessage) and message.tool_calls:
            results = fitted[index + 1:index + 1 + len(message.tool_calls)]
            assert {result.tool_call_id for result in results} == {call['id'] for call in message.tool_calls}


def test_web_modules_served(tmp_path):
    from fastapi.testclient import TestClient
    from artorias_server.api.app import create_http_app
    db=Store(tmp_path/'app.db')
    from artorias_local_app.bootstrap import create_app
    client=TestClient(create_app(tmp_path))
    page=client.get('/')
    assert page.status_code==200 and 'type="module"' in page.text
    assert '<style>' not in page.text and '<script>' not in page.text
    for file in ('app.js','api.js','events.js','workspace.js','reload.js','styles.css'):
        assert client.get('/static/'+('css/' if file.endswith('.css') else 'js/')+file).status_code==200
    assert client.get('/api/sessions').status_code==401
