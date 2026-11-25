"""Tool failure boundary: every tool outcome becomes a tool result message.

DeepAgents builds its ToolNode without ``handle_tool_errors``, so an exception
raised while executing a tool would abort the whole agent run.  This middleware
intercepts the tool-call handler and converts any failure into an error
``ToolMessage`` instead, so the model can observe and recover.  Approval
interrupts and cancellation must keep propagating: they are control flow, not
tool failures, and an invalid interrupt resume value must reach the framework
so the interrupt can be answered again.

It also guards against the model stubbornly retrying the same failing tool on
the same resource: once a (tool, resource) pair fails twice within one agent
graph, the error result is augmented with an explicit instruction to stop
retrying.
"""
import asyncio
import json

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from artorias_server.permissions import tool_call
from langgraph.errors import GraphBubbleUp, is_invalid_resume

_MAX_ERROR_CHARS = 4000
# After this many consecutive failures on the same resource, warn the model.
_REMIND_AFTER = 2


def _resource_key(call):
    """Stable identity for the resource a tool call targets."""
    args = call.get("args")
    if isinstance(args, dict):
        for key in ("path", "resource", "target", "working_directory"):
            if key in args:
                return f"{key}={args[key]!r}"
    try:
        return json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        return repr(args)


class ToolErrorBoundary(AgentMiddleware):
    """Convert tool invocation failures into error tool results, with retry guard."""

    def __init__(self):
        self.failures = {}

    def _key(self, request):
        call = request.tool_call if isinstance(request.tool_call, dict) else {}
        return (call.get("name"), _resource_key(call))

    def _reset(self, request):
        self.failures.pop(self._key(request), None)

    def _error(self, request, exc):
        key = self._key(request)
        count = self.failures.get(key, 0) + 1
        self.failures[key] = count
        call = request.tool_call if isinstance(request.tool_call, dict) else {}
        detail = f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_CHARS]
        if count >= _REMIND_AFTER:
            content = (
                f"工具调用失败：{detail}\n\n"
                f"【护栏】该工具已针对同一资源连续失败 {count} 次。"
                "请停止重复尝试相同的操作；请改用其他方法，或向用户说明失败原因并询问下一步。"
            )
        else:
            content = f"工具调用失败：{detail}"
        return ToolMessage(
            content=content,
            name=call.get("name"),
            tool_call_id=call.get("id"),
            status="error",
        )

    def wrap_tool_call(self, request, handler):
        token = tool_call.set(request.tool_call.get('id'))
        try:
            result = handler(request)
            self._reset(request)
            return result
        except (asyncio.CancelledError, GraphBubbleUp):
            raise
        except Exception as exc:
            if is_invalid_resume(exc):
                raise
            return self._error(request, exc)
        finally:
            tool_call.reset(token)

    async def awrap_tool_call(self, request, handler):
        token = tool_call.set(request.tool_call.get('id'))
        try:
            result = await handler(request)
            self._reset(request)
            return result
        except (asyncio.CancelledError, GraphBubbleUp):
            raise
        except Exception as exc:
            if is_invalid_resume(exc):
                raise
            return self._error(request, exc)
        finally:
            tool_call.reset(token)
