from fastapi.testclient import TestClient

from artorias_server.api.app import create_http_app
from artorias_server.storage.sqlite import Store


def test_timeline_bootstrap_pages_and_loads_complete_tool_details(tmp_path):
    store = Store(tmp_path / 'app.sqlite')
    session = store.session()
    other_session = store.session()

    older = store.submit(session, '早期消息', 'first')
    store.finish(dict(older, session=session), 'waiting', approval={'requests': [{'request_id': 'approve-old'}]})
    store.emit(session, None, 'workspace.selected', {'workspace_id': 'old'})

    middle = store.submit(session, '中间消息', 'middle')
    store.finish(dict(middle, session=session), 'completed', '中间回答')

    latest = store.submit(session, '最新消息', 'latest')
    for text in ('合', '并', '片段'):
        store.emit(session, latest['id'], 'assistant.delta', {'text': text})
    tool_result = {'call_id': 'call', 'name': 'run_command', 'stdout_tail': '完整命令输出'}
    store.emit(session, latest['id'], 'tool.result', tool_result)
    store.emit(session, latest['id'], 'assistant.final', {'text': '最终回答'})
    store.finish(dict(latest, session=session), 'completed', '最终回答')
    store.emit(session, latest['id'], 'run.completed', {})
    store.emit(session, None, 'workspace.selected', {'workspace_id': 'current'})
    other = store.submit(other_session, '其他会话', 'other')

    with TestClient(create_http_app(tmp_path, store)) as client:
        headers = {'x-artorias-token': client.get('/bootstrap').json()['token']}
        base = f'/api/sessions/{session}/timeline'
        initial = client.get(base, headers=headers).json()
        assert [turn['id'] for turn in initial['turns']] == [older['id'], latest['id']]
        old, new = initial['turns']
        assert old['events'][0]['kind'] == 'approval.requested'
        assert [item['kind'] for item in new['events']] == ['assistant.final']
        assert new['reply'] == '最终回答'
        assert [e['payload']['workspace_id'] for e in initial['events']] == ['current']

        store.emit(session, latest['id'], 'tool.result', {'content': 'snapshot 之后'})
        assert [e['seq'] for e in store.events(session, initial['cursor'])] == [initial['cursor'] + 2]

        history = client.get(base, params={'before': initial['next_before'], 'limit': 3}, headers=headers).json()
        assert [turn['id'] for turn in history['turns']] == [older['id'], middle['id']]
        assert [e['payload']['workspace_id'] for e in history['events']] == ['old']
        assert history['next_before'] is None

        details_url = f"{base}/{latest['id']}/events"
        first = client.get(details_url, params={'through': initial['cursor'], 'limit': 4}, headers=headers).json()
        assert first['events'][-1]['payload']['text'] == '合并片段'
        tail = client.get(details_url, params={'through': initial['cursor'], 'after': first['after']}, headers=headers).json()
        assert tail['events'][0]['payload'] == tool_result
        assert tail['after'] is None
        assert all(e['seq'] <= initial['cursor'] for e in tail['events'])
        assert client.get(f"{base}/{other['id']}/events", params={'through': initial['cursor']}, headers=headers).status_code == 404


def test_timeline_restores_only_in_flight_model_output(tmp_path):
    from artorias_server.sse.timeline import page

    store = Store(tmp_path / 'app.sqlite')
    session = store.session()
    store.submit(session, '输入', 'one')
    run = store.claim()
    rid = run['id']
    store.emit(session, rid, 'run.started', {})
    store.emit(session, rid, 'assistant.delta', {'text': '已结束的片段'})
    store.emit(session, rid, 'tool.call', {'call_id': 'call'})
    store.emit(session, rid, 'model.started', {'request_id': 'request'})
    store.emit(session, rid, 'assistant.delta', {'text': '当前'})
    store.emit(session, rid, 'assistant.delta', {'text': '输出'})
    events = page(store, session, None, 1)['turns'][0]['events']
    assert [e['kind'] for e in events] == ['model.started', 'assistant.delta']
    assert events[-1]['payload']['text'] == '当前输出'
