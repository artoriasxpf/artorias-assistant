import json
import sqlite3
import subprocess
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from artorias_search_service.engine.sqlite_index import SQLiteTextIndex
from artorias_server.integrations.search.models import IndexUnavailable, Scope
from artorias_search_service.api.app import create_app
from artorias_server.integrations.search.outbox import OutboxTextIndex
from artorias_server.integrations.search.client import SearchClient
from artorias_server.integrations.search.client import SearchProtocolError
from artorias_server.storage.sqlite import Store
from artorias_server.tasks.models import Item, Plan
from artorias_server.tasks.repository import TaskRepository
from artorias_server.integrations.search.tasks.projection import PROJECTION_VERSION
from artorias_server.integrations.search.worker import SearchSyncWorker


@pytest.fixture
def remote(tmp_path):
    path = tmp_path / 'search.sqlite'
    index = SQLiteTextIndex()
    app = create_app(path, token='secret', index=index)
    with TestClient(app) as server:
        def dispatch(request):
            response = server.request(request.method, request.url.path, content=request.content, headers=request.headers)
            return httpx.Response(response.status_code, content=response.content, headers=dict(response.headers))
        client = SearchClient('http://search', 'secret', transport=httpx.MockTransport(dispatch))
        store = Store(tmp_path / 'app.sqlite')
        repo = TaskRepository(store, text_index=OutboxTextIndex(client))
        worker = SearchSyncWorker(repo.search)
        yield store, repo, worker, client, server, path, index
        client.close()


def add_task(store, repo, session, goal='quartz'):
    store.submit(session, goal, str(len(store.query('SELECT id FROM runs'))))
    run = store.claim()
    repo.install(run, Plan(kind='tasks', items=[Item(key='one', goal=goal)]), [])
    return run, store.query('SELECT * FROM task_steps WHERE run=?', (run['id'],))[0]


def hits(client, session, text):
    return client.search(Scope('tasks', session), text, projection_version=PROJECTION_VERSION)


def drain(worker):
    for _ in range(30):
        if not worker.sync_once():
            return
    raise AssertionError('Outbox did not drain')


def test_remote_task_pipeline_and_eventual_consistency(remote):
    store, repo, worker, client, server, path, index = remote
    session = store.session()
    run, step = add_task(store, repo, session)
    assert not store.query("SELECT name FROM sqlite_master WHERE name='text_search_fts'")
    assert repo.candidates(session, 'quartz')[0]['recall_sources'] == ['recent']
    drain(worker)
    assert hits(client, session, 'quartz')[0].id == step['task']
    assert not hits(client, 'other-session', 'quartz')
    repo.deliver(run, step, 'newsummary')
    assert not hits(client, session, 'newsummary')
    drain(worker)
    assert hits(client, session, 'newsummary')[0].id == step['task']
    assert repo.candidates(session, 'newsummary')[0]['recall_sources'] == ['keyword', 'recent']


def test_outage_commit_retry_and_crash_after_remote_commit(remote, monkeypatch):
    store, repo, worker, client, *_ = remote
    session = store.session()
    run, step = add_task(store, repo, session)
    original = client.send

    def lost_ack(event):
        original(event)
        raise IndexUnavailable('response lost')

    monkeypatch.setattr(client, 'send', lost_ack)
    with pytest.raises(IndexUnavailable):
        worker.sync_once()
    assert store.query('SELECT * FROM search_outbox')
    repo.deliver(run, step, 'committedwhileoffline')
    assert store.query('SELECT summary FROM business_tasks')[0]['summary'] == 'committedwhileoffline'
    monkeypatch.setattr(client, 'send', original)
    drain(worker)
    assert hits(client, session, 'committedwhileoffline')
    assert not store.query('SELECT * FROM search_outbox')


def test_business_rollback_removes_event(remote, monkeypatch):
    store, repo, worker, client, *_ = remote
    session = store.session()
    run, step = add_task(store, repo, session)
    drain(worker)
    original = repo.search.index.upsert

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('after enqueue')

    monkeypatch.setattr(repo.search.index, 'upsert', fail)
    with pytest.raises(RuntimeError):
        repo.deliver(run, step, 'rollbackword')
    assert not store.query('SELECT * FROM search_outbox')
    assert store.query('SELECT summary FROM business_tasks')[0]['summary'] == ''
    assert not hits(client, session, 'rollbackword')


def test_version_upgrade_and_missing_service_index_recover(remote):
    store, repo, worker, client, server, path, index = remote
    session = store.session()
    run, step = add_task(store, repo, session)
    drain(worker)
    repo.deliver(run, step, 'latestsummary')
    index.tokenizer.version = 'new-tokenizer'
    drain(worker)
    assert hits(client, session, 'latestsummary')
    with sqlite3.connect(path) as conn:
        conn.execute('DROP TABLE text_search_fts')
    drain(worker)
    assert hits(client, session, 'latestsummary')


def test_snapshot_supersedes_pending_events_without_resetting_sequence(remote):
    store, repo, worker, client, *_ = remote
    session = store.session()
    run, step = add_task(store, repo, session)
    drain(worker)
    repo.deliver(run, step, 'snapshotword')
    before = store.query('SELECT next_seq FROM search_sync_state')[0]['next_seq']
    repo.rebuild_search()
    rows = store.query('SELECT * FROM search_outbox')
    assert len(rows) == 1 and rows[0]['seq'] > before
    drain(worker)
    assert hits(client, session, 'snapshotword')


def test_authorization_source_ownership_order_and_delete(remote):
    store, repo, worker, client, server, *_ = remote
    assert server.post('/v1/status', json={'collection':'tasks', 'projection_version':'v'}).status_code == 401
    with pytest.raises(SearchProtocolError, match='403'):
        client.status('private', 'v')
    session = store.session()
    run, step = add_task(store, repo, session)
    pending = [json.loads(row['payload']) for row in store.query('SELECT payload FROM search_outbox ORDER BY seq')]
    with pytest.raises(SearchProtocolError, match='sequence_gap'):
        client.send(pending[-1])
    drain(worker)
    old = dict(pending[0], source_id='foreign', seq=100)
    with pytest.raises(SearchProtocolError, match='source_conflict'):
        client.send(old)
    client.send(pending[0])  # Old snapshot cannot erase newer task content.
    assert hits(client, session, 'quartz')
    with store.tx() as conn:
        repo.search.index.delete(conn, Scope('tasks', session), [step['task']])
    drain(worker)
    assert not hits(client, session, 'quartz')


def test_remote_index_failure_rolls_back_cursor_and_snapshot(remote, monkeypatch):
    store, repo, worker, client, server, path, index = remote
    session = store.session()
    run, step = add_task(store, repo, session)
    drain(worker)
    before = client.status('tasks', PROJECTION_VERSION)['consumer']['last_seq']
    repo.deliver(run, step, 'mustrollback')
    repo.rebuild_search()
    original = index._upsert

    def broken(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('disk failure')

    monkeypatch.setattr(index, '_upsert', broken)
    with pytest.raises(RuntimeError, match='disk failure'):
        worker.sync_once()
    assert client.status('tasks', PROJECTION_VERSION)['consumer']['last_seq'] == before
    assert hits(client, session, 'quartz')
    assert not hits(client, session, 'mustrollback')
    monkeypatch.setattr(index, '_upsert', original)
    drain(worker)
    assert hits(client, session, 'mustrollback')


def test_network_outside_business_transaction_and_default_remote(remote, monkeypatch):
    store, repo, worker, client, *_ = remote
    monkeypatch.setenv('ARTORIAS_SEARCH_MODE', 'remote')
    monkeypatch.setenv('ARTORIAS_SEARCH_TOKEN', 'secret')
    default_repo = TaskRepository(store)
    assert default_repo.search.index.remote
    default_repo.search.index.client.close()
    session = store.session()
    add_task(store, repo, session)
    drain(worker)
    original = client.search

    def unlocked(*args, **kwargs):
        with sqlite3.connect(store.path, timeout=0) as conn:
            conn.execute('BEGIN IMMEDIATE')
        return original(*args, **kwargs)

    monkeypatch.setattr(client, 'search', unlocked)
    assert repo.candidates(session, 'quartz')


def test_client_import_does_not_load_tokenizer():
    result = subprocess.run([sys.executable, '-c',
        "import sys; import artorias_server.integrations.search.client; assert 'artorias_search_service' not in sys.modules and 'jieba' not in sys.modules"], capture_output=True)
    assert result.returncode == 0, result.stderr


def test_actual_transport_failure_and_auth_failure_are_distinct(remote, monkeypatch):
    store, repo, worker, client, *_ = remote
    session = store.session()
    run, step = add_task(store, repo, session)
    original = client.http.post

    def offline(*args, **kwargs):
        raise httpx.ConnectError('offline')

    monkeypatch.setattr(client.http, 'post', offline)
    repo.deliver(run, step, 'savedoffline')
    assert repo.candidates(session, 'savedoffline')[0]['recall_sources'] == ['recent']
    with pytest.raises(IndexUnavailable):
        worker.sync_once()
    monkeypatch.setattr(client.http, 'post', original)
    drain(worker)
    client.http.headers['Authorization'] = 'Bearer wrong'
    with pytest.raises(SearchProtocolError, match='401'):
        repo.candidates(session, 'savedoffline')


def test_update_during_snapshot_delivery_is_not_lost(remote, monkeypatch):
    store, repo, worker, client, *_ = remote
    session = store.session()
    run, step = add_task(store, repo, session)
    repo.rebuild_search()
    original = client.send

    def concurrent_update(event):
        repo.deliver(run, step, 'concurrentword')
        original(event)

    monkeypatch.setattr(client, 'send', concurrent_update)
    worker.sync_once()
    monkeypatch.setattr(client, 'send', original)
    assert len(store.query('SELECT * FROM search_outbox')) == 1
    drain(worker)
    assert hits(client, session, 'concurrentword')


def test_restart_reuses_source_and_pending_events(remote):
    store, repo, worker, client, *_ = remote
    session = store.session()
    run, step = add_task(store, repo, session)
    before = store.query('SELECT * FROM search_sync_state')
    restarted = TaskRepository(Store(store.path), text_index=OutboxTextIndex(client))
    assert store.query('SELECT * FROM search_sync_state') == before
    drain(SearchSyncWorker(restarted.search))
    assert hits(client, session, 'quartz')
