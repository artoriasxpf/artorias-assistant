import os
import socket
import subprocess
import sys
import time

import httpx
from fastapi.testclient import TestClient

from artorias_search_service.api.app import create_app


def event(seq=1, **changes):
    payload = dict(source_id='source', seq=seq, collection='tasks', projection_version='test-v1',
                   operation='snapshot', entries=[{'partition': 'session', 'document': {
                       'id': 'task', 'title': '中文搜索部署验证'}}])
    payload.update(changes)
    return payload


def test_event_sequence_and_idempotency(tmp_path):
    with TestClient(create_app(tmp_path / 'index.sqlite', token='secret')) as client:
        assert client.post('/v1/events', json=event()).status_code == 401
        client.headers['Authorization'] = 'Bearer secret'
        assert client.post('/v1/events', json=event()).json() == {'ack': 1, 'duplicate': False}
        assert client.post('/v1/events', json=event()).json() == {'ack': 1, 'duplicate': True}
        assert client.post('/v1/events', json=event(3, operation='upsert')).json()['detail'] == 'sequence_gap'
        assert client.post('/v1/events', json=event(2, source_id='other')).json()['detail'] == 'source_conflict'
        assert client.post('/v1/events', json=event(2, operation='delete')).json()['ack'] == 2
        query = dict(collection='tasks', partition='session', projection_version='test-v1', query='搜索')
        assert client.post('/v1/search', json=query).json() == {'hits': []}


def test_standalone_process_over_real_http(tmp_path):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = dict(os.environ, ARTORIAS_SEARCH_TOKEN='process-secret',
               ARTORIAS_SEARCH_DB=str(tmp_path / 'standalone.sqlite'))
    process = subprocess.Popen([sys.executable, '-m', 'artorias_search_service.main',
                                '--port', str(port)], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=1, trust_env=False,
                          headers={'Authorization': 'Bearer process-secret'}) as client:
            deadline = time.monotonic() + 10
            while True:
                try:
                    client.get('/readyz').raise_for_status()
                    break
                except httpx.TransportError:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
            assert client.post('/v1/events', json=event()).json()['ack'] == 1
            query = dict(collection='tasks', partition='session', projection_version='test-v1', query='搜索')
            assert client.post('/v1/search', json=query).json()['hits'][0]['id'] == 'task'
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
