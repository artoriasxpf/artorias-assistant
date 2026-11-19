"""Report when the user is waiting for a model response."""
from contextlib import contextmanager

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
            return await handler(request)
