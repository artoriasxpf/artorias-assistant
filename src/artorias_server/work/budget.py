"""Reserve every model request, including repair calls, against durable work budgets."""

import os

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from .repository import consume


class WorkBudgetExhausted(Exception):
    pass


class EndOfAttempt(Exception):
    pass


class WorkBudget(AgentMiddleware):
    def __init__(self, store, plan, attempt, tools):
        self.allowed = {t.name for t in tools} | {"read_file"}
        self.store = store
        self.plan = plan
        self.attempt = attempt
        self.limit = max(1, int(os.getenv("ARTORIAS_WORK_ATTEMPT_CALLS", "24")))

    def reserve(self):
        with self.store.tx() as db:
            usage = db.execute("SELECT calls FROM work_attempts WHERE id=?", (self.attempt,)).fetchone()
            if usage["calls"] >= self.limit:
                raise EndOfAttempt
            if not consume(db, self.plan, self.attempt):
                raise WorkBudgetExhausted

    async def awrap_model_call(self, request, handler):
        self.reserve()
        tools = [tool for tool in request.tools if getattr(tool, "name", None) in self.allowed]
        return await handler(request.override(tools=tools))

    async def awrap_tool_call(self, request, handler):
        call = request.tool_call
        if call["name"] not in self.allowed:
            return ToolMessage(
                content="当前工作单元未开放此工具",
                name=call["name"],
                tool_call_id=call["id"],
                status="error",
            )
        return await handler(request)
