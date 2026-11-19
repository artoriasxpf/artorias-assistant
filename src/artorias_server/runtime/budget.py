"""Fail closed on oversized requests without breaking tool call/response pairing.

This is a UTF-8 serialized-byte guard, not a provider-token accounting guarantee.
A deployment should calibrate its limit against the selected model's tokenizer.
"""
import json
import os
from langchain.agents.middleware import AgentMiddleware
from langchain_core.utils.function_calling import convert_to_openai_tool


def check_budget(payload, limit=None):
    limit = limit or int(os.getenv('ARTORIAS_REQUEST_MAX_BYTES', '160000'))
    size = len(json.dumps(payload, ensure_ascii=False, default=str).encode('utf-8'))
    if size > limit:
        raise ValueError(f'Model request exceeds byte budget ({size}>{limit}); narrow the task or read shorter results')
    return size


class RequestBudget(AgentMiddleware):
    async def awrap_model_call(self, request, handler):
        payload = {
            'system': request.system_message.model_dump() if request.system_message else None,
            'messages': [m.model_dump() for m in request.messages],
            'tools': [convert_to_openai_tool(t) for t in request.tools],
            'model_settings': request.model_settings,
        }
        check_budget(payload)
        return await handler(request)
