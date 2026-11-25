"""Expose workspace executors as DeepAgents tools."""
from typing import Annotated, Literal

from langchain_core.tools import InjectedToolCallId, tool
from langgraph.types import interrupt
from artorias_server.permissions import tool_call
from pydantic import BaseModel, ConfigDict, Field

from artorias_shared.execution.models import InputResource, OutputPolicy, PythonRequest
from .executor import WorkspaceToolExecutor


class RunPythonArguments(PythonRequest):
    tool_call_id: Annotated[str, InjectedToolCallId]


class DirectoryArguments(BaseModel):
    model_config = ConfigDict(extra='forbid')
    path: str = Field(default='.', description='Workspace relative directory, authorized local absolute directory, or workspace://ID/path.')


def build_tools(ws, emit, *, sandbox=None, execution_service=None):
    executor = WorkspaceToolExecutor(ws, sandbox=sandbox, execution_service=execution_service)

    def file_operation(path, action, directory, operation):
        call_id = tool_call.get()
        if not call_id:
            return operation()
        return executor.file_operation(call_id, path, action, directory, operation, interrupt)

    @tool
    def project_tree(
        path: str = '.',
        max_depth: Annotated[int, Field(ge=0)] = 2,
        max_entries: Annotated[int, Field(ge=1, le=1000)] = 200,
        include_hidden: bool = False,
    ) -> dict:
        """Browse an authorized directory tree. Defaults to the linked workspace,
        two levels and 200 entries. Hidden names are excluded by default.
        If truncated is true, narrow the path or increase max_entries (up to 1000).
        """
        return file_operation(path, 'read', True, lambda: executor.tree(
            path, max_depth=max_depth, max_entries=max_entries, include_hidden=include_hidden,
        ))

    @tool(args_schema=DirectoryArguments)
    def project_list(path: str = '.') -> dict:
        """List all direct files and folders in one authorized directory, including
        hidden entries. No recursion, pagination or truncation. Use project_tree
        first for large directories. Both tools omit symlinks and special files.
        """
        return file_operation(path, 'read', True, lambda: executor.list_directory(path))

    @tool
    def project_read(path: str, offset: int = 0, limit: int = 2000,
                     expected_version: str | None = None) -> dict:
        """Read one UTF-8 page from an authorized file. For the next page, pass
        next_offset as offset and version as expected_version. For analysis, use
        at most 3 reads per batch and limit each to 2000 characters. Record concise
        findings with source paths and page ranges before reading the next batch.
        """
        return file_operation(path, 'read', False,
                              lambda: executor.read(path, offset, limit, expected_version))

    @tool
    def project_write(path: str, content: str) -> dict:
        """Create or replace an authorized UTF-8 file with version history.
        Read an existing file before replacing it.
        """
        return file_operation(path, 'write', False, lambda: executor.write(path, content))

    @tool
    def project_edit(path: str, old: str, new: str) -> dict:
        """Replace one uniquely matching string in an authorized UTF-8 file."""
        return file_operation(path, 'write', False, lambda: executor.edit(path, old, new))

    @tool
    def project_delete(path: str) -> str | dict:
        """Delete an authorized file while keeping its previous version."""
        return file_operation(path, 'write', False, lambda: executor.delete(path))

    @tool
    def project_authorize(path: str, access: Literal['read', 'write'] = 'write') -> dict:
        """Ask for access to a local absolute directory and its descendants.
        Request read for listing or reading, and write for changes. Retry the file
        operation after approval. Do not repeat a denied authorization.
        """
        return executor.authorize(path, access, interrupt, tool_call.get())

    @tool(args_schema=RunPythonArguments)
    async def run_python(
        path: str, tool_call_id: Annotated[str, InjectedToolCallId],
        args: list[str] | None = None, mode: str = 'isolated',
        inputs: list[InputResource] | None = None, output: OutputPolicy | None = None,
        working_directory: str | None = None, code_directory: str | None = None,
        allow_delete: bool = False, execution_hint: str = 'auto',
        environment: str = 'python-default', timeout_seconds: int = 120,
    ) -> dict:
        """Run an authorized Python script after approval. Auto uses the linked
        workspace's execution environment. Inputs are staged under /inputs,
        code under /app, and outputs under /outputs. workspace_copy requires
        working_directory. Existing output overwrites require if_unchanged
        and expected_files. Returns execution and publication statuses.
        """
        request = PythonRequest(
            path=path, args=args or [], mode=mode, inputs=inputs or [],
            output=output or OutputPolicy(), working_directory=working_directory,
            code_directory=code_directory, allow_delete=allow_delete, execution_hint=execution_hint,
            environment=environment, timeout_seconds=timeout_seconds,
        )
        return await executor.run_python(tool_call_id, request, interrupt, emit)

    return [project_tree, project_list, project_read, project_write, project_edit, project_delete, project_authorize, run_python]
