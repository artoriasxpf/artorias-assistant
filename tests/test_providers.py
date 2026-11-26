import asyncio
import json

import httpx
import pytest
from artorias_server.model_provider.settings import ModelSettings
from artorias_server.model_provider.factory import create_model
from artorias_server.runtime.service import text_content

@pytest.mark.parametrize('provider,module,cls',[('openai','langchain_openai','ChatOpenAI'),('deepseek','langchain_deepseek','ChatDeepSeek'),('anthropic','langchain_anthropic','ChatAnthropic')])
def test_real_constructor(provider,module,cls,monkeypatch):
    # Constructor test: do not inherit execution-host proxy transports.
    import os
    for key in list(os.environ):
        if key.lower().endswith("_proxy"):monkeypatch.delenv(key,raising=False)
    import importlib
    model=create_model(ModelSettings(provider,'test-model','test-key'))
    assert isinstance(model,getattr(importlib.import_module(module),cls))
    assert hasattr(model,'bind_tools')

def test_env_isolation(monkeypatch):
    monkeypatch.setenv('ARTORIAS_MODEL_PROVIDER','deepseek');monkeypatch.setenv('ARTORIAS_MODEL','test')
    monkeypatch.delenv('DEEPSEEK_API_KEY',raising=False);monkeypatch.setenv('OPENAI_API_KEY','wrong-provider')
    with pytest.raises(ValueError,match='DEEPSEEK_API_KEY'):ModelSettings.from_env()
    monkeypatch.setenv('DEEPSEEK_API_KEY','secret');config=ModelSettings.from_env()
    assert config.api_key=='secret' and 'secret' not in repr(config)
    assert config.base_url=='https://api.deepseek.com'

def test_anthropic_blocks():
    assert text_content([{'type':'thinking','thinking':'private'},{'type':'text','text':'hello'},{'type':'text','text':' world'}])=='hello world'


def test_deepseek_round_trips_streamed_reasoning_after_tool_call():
    from deepagents import create_deep_agent
    from langchain_core.tools import tool
    from artorias_server.model_provider.deepseek import DeepSeekChatModel

    @tool
    def lookup() -> str:
        """Return a fixture value."""
        return 'fixture result'

    def stream(*deltas, finish_reason):
        chunks = [
            {'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]}
            for delta in deltas
        ]
        chunks.append({'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish_reason}]})
        return ''.join(f'data: {json.dumps(chunk)}\n\n' for chunk in chunks) + 'data: [DONE]\n\n'

    replies = iter([
        stream({'role': 'assistant', 'reasoning_content': ' First fragment\n'},
               {'reasoning_content': 'Second fragment '},
               {'tool_calls': [{'index': 0, 'id': 'call_fixture', 'type': 'function',
                                'function': {'name': 'lookup', 'arguments': '{}'}}]},
               finish_reason='tool_calls'),
        stream({'role': 'assistant', 'content': 'Done.'}, finish_reason='stop'),
    ])
    requests = []

    def dispatch(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, text=next(replies))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(dispatch)) as client:
            model = DeepSeekChatModel(model='deepseek-reasoner', api_key='offline-test',
                                      http_async_client=client, streaming=True)
            agent = create_deep_agent(model=model, tools=[lookup])
            return await agent.ainvoke({'messages': [{'role': 'user', 'content': 'Use lookup.'}]})

    result = asyncio.run(run())
    assert result['messages'][-1].content == 'Done.'
    assert len(requests) == 2
    assert all(request['stream'] and request['tools'] for request in requests)
    assistant, tool_result = requests[1]['messages'][-2:]
    assert assistant['role'] == 'assistant'
    assert assistant['reasoning_content'] == ' First fragment\nSecond fragment '
    assert assistant['tool_calls'][0]['id'] == tool_result['tool_call_id'] == 'call_fixture'
    assert tool_result['content'] == 'fixture result'
    assert all('reasoning_content' not in message for message in requests[0]['messages'])


def test_deepseek_budget_compaction_does_not_invent_assistant_turns():
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from artorias_server.runtime.budget import _fit_messages, _size

    def tool_batch(call_id, argument, reasoning):
        return [
            AIMessage(content='', additional_kwargs={'reasoning_content': reasoning},
                      tool_calls=[{'id': call_id, 'name': 'lookup', 'args': {'value': argument}}]),
            ToolMessage(content='result', tool_call_id=call_id),
        ]

    latest = tool_batch('current', 'small input', 'original current reasoning')
    messages = [HumanMessage(content='Use lookup.'),
                *tool_batch('old', 'x' * 20000, 'old reasoning'), *latest]
    compacted = _fit_messages(messages, {}, 5000)
    assert _size({'messages': [message.model_dump() for message in compacted]}) <= 5000
    assert compacted[-2:] == latest

    model = create_model(ModelSettings('deepseek', 'deepseek-reasoner', 'offline-test'))
    payload = model._get_request_payload(compacted)['messages']
    assert payload[1]['role'] == 'user'
    assert '历史工具调用摘录' in payload[1]['content']
    assistant, = [message for message in payload if message['role'] == 'assistant']
    assert assistant['reasoning_content'] == 'original current reasoning'
    assert assistant['tool_calls'][0]['id'] == payload[-1]['tool_call_id'] == 'current'

    # Even the last-resort fallback must not drop the current reasoning exchange.
    assert _fit_messages(messages, {}, 100)[-2] is latest[0]
