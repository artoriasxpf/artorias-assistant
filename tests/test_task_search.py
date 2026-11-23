import sqlite3
import subprocess
import sys

import pytest

from artorias_search import Scope, SearchHit
from artorias_server.storage.sqlite import Store
from artorias_server.tasks.models import Item, Plan, RequirementOperation, RequirementPatch
from artorias_server.tasks.repository import TaskRepository
from artorias_server.tasks.search import PROJECTION_VERSION


@pytest.fixture
def tasks(tmp_path):
    store = Store(tmp_path / 'app.sqlite')
    return store, TaskRepository(store), store.session()


def create(tasks, goal='originalword', **fields):
    store, repo, session = tasks
    store.submit(session, goal, str(len(store.query('SELECT id FROM runs'))))
    run = store.claim()
    repo.install(run, Plan(kind='tasks', items=[Item(key='step', goal=goal, **fields)]), [])
    step = store.query('SELECT * FROM task_steps WHERE run=?', (run['id'],))[0]
    return run, step


def search(tasks, query):
    store, repo, session = tasks
    with store.tx() as conn:
        return [hit.id for hit in repo.search.index.search(conn, Scope('tasks', session), query,
                    projection_version=PROJECTION_VERSION)]


def test_recall_beyond_200_order_sources_dedup_and_session(tasks):
    store, repo, session = tasks
    foreign = store.session()
    with store.tx() as conn:
        for i in range(215):
            conn.execute('''INSERT INTO business_tasks(id,session,goal,constraints,criterias,status,updated_at)
                VALUES(?,?,?,'{}','{}','open',?)''',
                (f'{i:032x}', foreign if i == 214 else session,
                 'quartz' if i in (0, 214) else 'unrelated', f'2026-01-01T00:00:00.{i:03}Z'))
            repo.search.update(conn, f'{i:032x}')
    candidates = repo.candidates(session, f'引用{2:032x}、{1:032x}，查找quartz，{214:032x}，{2:032x}')
    assert [row['id'] for row in candidates[:3]] == [f'{2:032x}', f'{1:032x}', f'{0:032x}']
    assert candidates[0]['recall_sources'] == ['explicit']
    assert candidates[2]['recall_sources'] == ['keyword']
    assert candidates[3]['id'] == f'{213:032x}'
    assert candidates[3]['recall_sources'] == ['recent']
    assert len(candidates) == len({row['id'] for row in candidates}) == 11
    assert all(row['id'] != f'{214:032x}' for row in candidates)
    overlap = repo.candidates(session, f'{213:032x}')[0]
    assert overlap['recall_sources'] == ['explicit', 'recent']
    capped = repo.candidates(session, ' '.join(f'{i:032x}' for i in range(30)))
    assert [row['id'] for row in capped] == [f'{i:032x}' for i in range(20)]
    assert len(repo.candidates(session, '的 和 我')) == 8


@pytest.mark.parametrize('scope', ['persistent', 'execution'])
def test_requirement_projection_and_delivery_updates(tasks, scope):
    store, repo, session = tasks
    run, first = create(tasks, constraints=['oldconstraint'], criteria=['oldcriterion'])
    tid = first['task']
    assert search(tasks, 'originalword') == [tid]
    assert search(tasks, 'oldconstraint') == [tid]
    assert search(tasks, 'oldcriterion') == [tid]
    repo.deliver(run, first, 'oldsummary')
    assert search(tasks, 'oldsummary') == [tid]
    store.finish(run, 'completed')
    store.submit(session, 'replacewords', 'revision')
    changed = store.claim()
    patch = RequirementPatch(scope=scope, expected_revision=0, operations=[
        RequirementOperation(op='replace', field='goal', value='newgoal', evidence='replacewords'),
        RequirementOperation(op='replace', field='constraints', id='constraint_1', value='newconstraint', evidence='replacewords'),
        RequirementOperation(op='replace', field='criteria', id='criterion_1', value='newcriterion', evidence='replacewords'),
    ])
    repo.install(changed, Plan(kind='tasks', items=[Item(key='next', goal='execute', relation='refine',
        task_id=tid, task_patch=patch)]), repo.candidates(session, tid))
    for old, new in [('originalword', 'newgoal'), ('oldconstraint', 'newconstraint'), ('oldcriterion', 'newcriterion')]:
        assert search(tasks, old) == ([] if scope == 'persistent' else [tid])
        assert search(tasks, new) == ([tid] if scope == 'persistent' else [])
    step = store.query('SELECT * FROM task_steps WHERE run=?', (changed['id'],))[0]
    summary = 'beginword ' + 'x ' * 8000 + ' endword'
    repo.deliver(changed, step, summary)
    assert search(tasks, 'oldsummary') == []
    assert search(tasks, 'beginword endword') == [tid]
    row = store.query('SELECT * FROM text_search_documents WHERE document_id=?', (tid,))[0]
    assert len(row['body']) == 12000
    assert row['body'].startswith('beginword') and row['body'].endswith('endword')
    assert store.query('SELECT summary FROM business_tasks WHERE id=?', (tid,))[0]['summary'] == summary
    repo.deliver(changed, step, 'duplicateignored')
    assert search(tasks, 'duplicateignored') == []


def test_write_failure_rolls_back_plan_and_delivery(tasks, monkeypatch):
    store, repo, session = tasks
    run, step = create(tasks)
    original = repo.search.index.upsert

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('index failure after write')

    monkeypatch.setattr(repo.search.index, 'upsert', fail)
    with pytest.raises(RuntimeError, match='index failure'):
        repo.deliver(run, step, 'newsummary')
    assert store.query('SELECT summary,execution_count FROM business_tasks')[0] == {'summary': '', 'execution_count': 0}
    assert store.query('SELECT status FROM task_steps')[0]['status'] == 'pending'
    assert search(tasks, 'newsummary') == []
    store.finish(run, 'completed')
    store.submit(session, 'newtask', 'failure')
    failed_run = store.claim()
    with pytest.raises(RuntimeError, match='index failure'):
        repo.install(failed_run, Plan(kind='tasks', items=[Item(key='new', goal='newtask')]), [])
    assert len(store.query('SELECT * FROM business_tasks')) == 1
    assert not store.query('SELECT * FROM task_plans WHERE run=?', (failed_run['id'],))
    assert not store.query('SELECT * FROM task_steps WHERE run=?', (failed_run['id'],))
    assert search(tasks, 'newtask') == []


def test_first_build_restart_version_upgrade_and_manual_rebuild(tasks, tmp_path):
    store, repo, session = tasks
    run, step = create(tasks)
    # Simulate authoritative tasks predating deployment, and stale projection metadata.
    with store.tx() as conn:
        conn.execute('DELETE FROM text_search_collections')
        conn.execute("UPDATE business_tasks SET goal='reindexedword'")
    repo = TaskRepository(Store(store.path))
    tasks = store, repo, session
    assert search(tasks, 'reindexedword') == [step['task']]
    metadata = store.query('SELECT * FROM text_search_collections')
    TaskRepository(Store(store.path))
    assert store.query('SELECT * FROM text_search_collections') == metadata
    with store.tx() as conn:
        conn.execute("UPDATE business_tasks SET goal='versionchange'")
        conn.execute("UPDATE text_search_collections SET tokenizer_version='old'")
    repo = TaskRepository(store)
    assert search((store, repo, session), 'versionchange') == [step['task']]
    with store.tx() as conn:
        conn.execute("UPDATE business_tasks SET goal='manualrepair'")
    result = subprocess.run([sys.executable, 'scripts/rebuild_task_search.py', '--data', str(tmp_path)],
                            capture_output=True, text=True, check=True)
    assert 'Rebuilt 1 Task documents' in result.stdout
    assert search((store, repo, session), 'manualrepair') == [step['task']]


def test_degraded_recall_recovery_and_unrelated_errors(tasks, caplog, monkeypatch):
    store, repo, session = tasks
    run, step = create(tasks)
    with store.tx() as conn:
        conn.execute('DROP TABLE text_search_fts')
    candidates = repo.candidates(session, step['task'])
    assert candidates[0]['recall_sources'] == ['explicit', 'recent']
    assert 'task.recall degraded' in caplog.text
    assert repo.rebuild_search() == 1
    assert search(tasks, 'originalword') == [step['task']]

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError('database is locked')

    monkeypatch.setattr(repo.search.index, 'search', broken)
    with pytest.raises(sqlite3.OperationalError, match='locked'):
        repo.candidates(session, 'originalword')


def test_hydration_rechecks_session_and_current_revision(tasks, monkeypatch):
    store, repo, session = tasks
    run, step = create(tasks)
    with store.tx() as conn:
        conn.execute("UPDATE business_tasks SET revision=7,goal='newauthoritative'")
        conn.execute('''INSERT INTO business_tasks(id,session,goal,constraints,criterias,status)
            VALUES('foreign','other','foreign','{}','{}','open')''')
    monkeypatch.setattr(repo.search.index, 'search', lambda *args, **kwargs: [
        SearchHit('foreign', -5), SearchHit('deleted', -4), SearchHit(step['task'], -3)])
    candidate, = repo.candidates(session, 'anything')
    assert candidate['id'] == step['task'] and candidate['revision'] == 7
    assert candidate['goal'] == 'newauthoritative'
