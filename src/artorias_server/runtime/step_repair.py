"""Retry a malformed Step submission using the existing execution transcript."""

from langchain_core.messages import HumanMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ValidationError

from .budget import _fit_messages, _request_limit, check_budget
from .model_activity import track_model_request

MAX_REPAIRS = 2


def submission_error(exc):
    if isinstance(exc, ValidationError):
        return "；".join(
            f"{'.'.join(map(str, issue['loc']))}: {issue['msg']}"
            for issue in exc.errors(include_input=False, include_url=False)[:12]
        )
    return str(exc)[:1200]


async def repair_submission(
    model,
    tool,
    validate,
    history,
    emit,
    problem,
    *,
    reserve=None,
    instructions=None,
    attempts=MAX_REPAIRS,
    stage="step_repair",
):
    if instructions is None:
        instructions = (
            "修正本步交付。只调用一次 finish_step，提交完整参数。"
            "沿用已有输出和工具结果，切勿重新执行工作。"
            "没有足够证据的标准用 partial 或 unmet 并解释。"
        )
    envelope = {"tools": [convert_to_openai_tool(tool)]}
    repair_model = model.bind_tools([tool])
    limit = _request_limit()
    for _ in range(attempts):
        instruction = HumanMessage(content=(instructions + f"上次提交的问题：{problem}"))
        messages = _fit_messages([*history, instruction], envelope, limit)
        check_budget({**envelope, "messages": [msg.model_dump() for msg in messages]}, limit)
        if reserve is not None:
            reserve()
        with track_model_request(emit, stage):
            reply = await repair_model.ainvoke(messages)
        calls = reply.tool_calls
        if reply.invalid_tool_calls or len(calls) != 1 or calls[0]["name"] != tool.name:
            problem = f"需要一次参数为有效 JSON 的 {tool.name} 调用"
            continue
        try:
            return validate(calls[0]["args"])
        except ValueError as exc:
            problem = submission_error(exc)
    raise ValueError(f"{tool.name} 修正 {attempts} 次后仍无法交付：{problem}")
