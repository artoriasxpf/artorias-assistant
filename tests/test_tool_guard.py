"""Tool failure boundary: failures become tool results and retries are guarded."""
import asyncio

from langchain_core.messages import ToolMessage

from artorias_server.runtime.tool_errors import ToolErrorBoundary


class Request:
    tool_call = {'name': 'project_read', 'id': 'c1', 'args': {'path': 'missing.txt'}}


def failing(request):
    raise FileNotFoundError('missing.txt')


def test_retry_guard_triggers_after_two_failures():
    boundary = ToolErrorBoundary()
    first = boundary.wrap_tool_call(Request(), failing)
    second = boundary.wrap_tool_call(Request(), failing)
    third = boundary.wrap_tool_call(Request(), failing)
    assert first.status == 'error' and '【护栏】' not in first.content
    assert second.status == 'error' and '连续失败 2 次' in second.content
    assert third.status == 'error' and '连续失败 3 次' in third.content


def test_success_resets_guard():
    boundary = ToolErrorBoundary()
    assert boundary.wrap_tool_call(Request(), failing).status == 'error'
    assert '连续失败 2 次' in boundary.wrap_tool_call(Request(), failing).content

    def ok(request):
        return ToolMessage(content='ok', tool_call_id='c1')

    boundary.wrap_tool_call(Request(), ok)
    again = boundary.wrap_tool_call(Request(), failing)
    assert again.status == 'error' and '【护栏】' not in again.content


def test_different_resources_are_tracked_independently():
    boundary = ToolErrorBoundary()
    assert boundary.wrap_tool_call(Request(), failing).status == 'error'
    other = Request()
    other.tool_call = {'name': 'project_read', 'id': 'c2', 'args': {'path': 'other.txt'}}
    first_other = boundary.wrap_tool_call(other, failing)
    assert first_other.status == 'error' and '【护栏】' not in first_other.content
    # Same original resource again still counts towards its own sequence.
    assert '连续失败 2 次' in boundary.wrap_tool_call(Request(), failing).content


def test_async_guard():
    boundary = ToolErrorBoundary()

    async def afailing(request):
        raise FileNotFoundError('missing.txt')

    first = asyncio.run(boundary.awrap_tool_call(Request(), afailing))
    second = asyncio.run(boundary.awrap_tool_call(Request(), afailing))
    assert first.status == 'error' and '【护栏】' not in first.content
    assert '连续失败 2 次' in second.content
