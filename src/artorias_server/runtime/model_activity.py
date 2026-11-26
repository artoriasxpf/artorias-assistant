"""Report when the user is waiting for a model response."""
from contextlib import contextmanager
import json

from langchain.agents.middleware import AgentMiddleware
from langgraph.config import get_stream_writer

from artorias_shared.ids import uid


@contextmanager
def track_model_request(emit, stage):
    payload = {'request_id': uid(), 'stage': stage}
    emit('model.started', payload)
    try:
        yield
    finally:
        emit('model.finished', payload)


class ModelActivity(AgentMiddleware):
    async def awrap_model_call(self, request, handler):
        writer = get_stream_writer()
        with track_model_request(lambda kind, payload: writer({'kind': kind, 'payload': payload}), 'agent'):
            response = await handler(request)
            messages = [message for message in response.result if message.type == 'ai']
            usages = [message.usage_metadata for message in messages if message.usage_metadata]
            output_bytes = sum(len(json.dumps(
                [message.content, message.tool_calls], ensure_ascii=False, default=str,
            ).encode('utf-8')) for message in messages)
            writer({'kind': 'runtime.usage_metrics', 'payload': {
                'input_tokens': sum(usage['input_tokens'] for usage in usages) if usages else None,
                'output_tokens': sum(usage['output_tokens'] for usage in usages) if usages else None,
                'output_bytes': output_bytes,
            }})
            return response
