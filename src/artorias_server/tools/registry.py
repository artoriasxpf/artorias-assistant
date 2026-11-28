"""Expose workspace executors as DeepAgents tools."""
import os
from typing import Annotated, Literal
from time import monotonic

from langchain_core.tools import InjectedToolCallId, tool
from langgraph.types import interrupt
from artorias_server.permissions import tool_call
from pydantic import BaseModel, ConfigDict, Field

from artorias_shared.execution.models import CommandRequest, InputResource, OutputPolicy, PythonRequest
from .executor import WorkspaceToolExecutor
from .change_plans import ChangePlans, PlanArguments
from .project_analysis import (
    InventoryArguments, ProjectAnalysis, ReadArguments, SearchArguments, stopped,
)
from .files import json_bytes


class RunPythonArguments(PythonRequest):
    tool_call_id: Annotated[str, InjectedToolCallId]
    output: OutputPolicy | None = Field(
        default=None, description='Artifact publication policy for isolated or workspace_copy only.',
    )


class CommandArguments(CommandRequest):
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

    analysis = ProjectAnalysis(ws, executor.files)
    changes = ChangePlans(ws)

    def measure(name, operation):
        started = monotonic()
        result = operation()
        if result.get('rejected'):
            result = stopped('permission_denied')
        emit('runtime.tool_metrics', {
            'name': name, 'call_id': tool_call.get(),
            'duration_ms': round((monotonic() - started) * 1000, 2),
            'serialized_bytes': len(json_bytes(result)),
            'hits': len(result.get('hits', [])),
            'coverage': result['coverage'], 'stop_reason': result['stop_reason'],
        })
        return result

    @tool(args_schema=InventoryArguments)
    def project_inventory(**kwargs) -> dict:
        """Summarize directory contents using metadata only. Paths in samples are
        relative to root. For the next page, repeat the options with next_cursor.
        Set include_hidden=true for dotfiles. Report coverage and exclusions.
        """
        args = InventoryArguments(**kwargs)
        if args.snapshot_page:
            return file_operation(args.root, 'read', True, lambda: analysis.snapshot_page(args))
        return measure('project_inventory', lambda: file_operation(
            args.root, 'read', True, lambda: analysis.inventory(args),
        ))

    @tool(args_schema=SearchArguments)
    def project_search(**kwargs) -> dict:
        """Search authorized paths by name, literal text, or line-oriented regex.
        Return matching locations and short excerpts. Set include_hidden=true for dotfiles.
        Read source ranges to verify.
        If next_cursor is present, repeat the original options to continue.
        """
        args = SearchArguments(**kwargs)
        if args.snapshot_page:
            return file_operation(args.root or '.', 'read', True, lambda: analysis.snapshot_page(args))
        inventory = analysis.inventory_options(args.inventory_id) if args.inventory_id else None
        root = args.root or (inventory['root'] if inventory else '.')
        return measure('project_search', lambda: stopped('cursor_expired') if args.inventory_id and not inventory
                       else file_operation(root, 'read', True, lambda: analysis.search(args, inventory)))

    @tool(args_schema=ReadArguments)
    def project_read_many(**kwargs) -> dict:
        """Read up to eight authorized text ranges. Use 1-based lines or 0-based
        Unicode character offsets. Resume with next_start and expected_version.
        Inspect each item's status and the overall coverage. If one line exceeds the
        byte budget, switch to character offsets.
        """
        args = ReadArguments(**kwargs)
        paths = dict.fromkeys(item.path for item in args.items)
        return measure('project_read_many', lambda: analysis.read_many(
            args, executor.read_permissions(tool_call.get(), paths, interrupt),
        ))

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
                     expected_version: str | None = None, unit: Literal["unicode_character", "byte"] = "unicode_character") -> dict:
        """Read one UTF-8 page from an authorized file. For the next page, pass
        next_offset as offset and version as expected_version. For large files use byte pages
        (limit 4–4096) and continue with next_start and the returned version.
        """
        return file_operation(path, 'read', False,
                              lambda: analysis.byte_page(path, offset, limit, expected_version) if unit == 'byte'
                              else executor.read(path, offset, limit, expected_version))

    @tool
    def project_write(path: str, content: str) -> dict:
        """Create or replace an authorized UTF-8 file with version history.
        Read an existing file before replacing it.
        """
        return file_operation(path, 'write', False, lambda: executor.write(path, content))

    @tool
    def project_edit(path: str, old: str, new: str, expected_version: str | None = None) -> dict:
        """Replace one uniquely matching string in an authorized UTF-8 file."""
        return file_operation(path, 'write', False, lambda: executor.edit(path, old, new, expected_version))

    @tool
    def project_delete(path: str) -> str | dict:
        """Delete an authorized file while keeping its previous version."""
        return file_operation(path, 'write', False, lambda: executor.delete(path))

    @tool
    def project_move(source: str, destination: str, expected_source_version: str | None = None) -> dict:
        """Move or rename one regular file on the same client, including across
        authorized local workspaces. Both paths require write access. Destination
        is the full new file path, must not exist, and its parent must exist.
        No file content is returned. Pass a known source version to check for edits.
        The change_id identifies the move for undo.
        """
        return executor.group_operation(
            tool_call.get(), [source, destination], 'write',
            ['move', source, destination, expected_source_version],
            lambda: executor.move(source, destination, expected_source_version), interrupt,
        )

    @tool
    def project_mkdir(path: str, parents: bool = False) -> dict:
        """Create an authorized local directory. Set parents=true to create missing
        ancestors inside the authorized directory scope. Existing directories are safe to retry.
        """
        return file_operation(path, 'write', True, lambda: ws.mkdir(path, parents=parents))

    @tool(args_schema=PlanArguments)
    def project_change_plan(**kwargs) -> dict:
        """Preview a versioned edit of existing UTF-8 files. Replace uses explicit
        paths and match counts. Patch uses zero-based half-open Unicode character
        offsets from the specified versions. Returns diffs and a plan_id without writing workspace files.
        """
        request = PlanArguments.model_validate(kwargs).request
        return executor.group_operation(
            tool_call.get(), [item.path for item in request.files], 'read',
            'change_preview', lambda: changes.preview(request), interrupt,
        )

    @tool
    def project_change_apply(plan_id: str) -> dict:
        """Apply a previously previewed plan in this Session and workspace.
        Reports conflicts, partial writes and per-file change IDs for undo.
        """
        items = changes.load(plan_id)
        return executor.group_operation(
            tool_call.get(), [item['path'] for item in items], 'write',
            ['change_apply', plan_id], lambda: changes.apply(plan_id, items), interrupt,
        )

    @tool
    def project_authorize(path: str, access: Literal['read', 'write'] = 'write') -> dict:
        """Ask for access to a local absolute directory and its descendants.
        Request read for listing or reading, and write for changes. Retry the file
        operation after approval. Do not repeat a denied authorization.
        """
        return executor.authorize(path, access, interrupt, tool_call.get())

    @tool(args_schema=RunPythonArguments)
    async def run_python(
        tool_call_id: Annotated[str, InjectedToolCallId], path: str | None = None,
        code: str | None = None, args: list[str] | None = None, mode: str = 'isolated',
        inputs: list[InputResource] | None = None, output: OutputPolicy | None = None,
        working_directory: str | None = None, code_directory: str | None = None,
        allow_delete: bool = False, execution_hint: str = 'auto',
        environment: str = 'python-default', timeout_seconds: int = 120,
        dependencies: list[str] | None = None,
    ) -> dict:
        """Run Python from exactly one of path (workspace script) or code (inline source).
        workspace_ro reads the live workspace; workspace_rw writes it immediately. Use relative paths.
        Execution requires approval. Auto uses the linked
        workspace's execution environment. Inputs are staged under /inputs,
        code under /app, and outputs under /outputs. workspace_copy requires
        working_directory. Prefer ARTORIAS_INPUT_DIR, ARTORIAS_OUTPUT_DIR and
        ARTORIAS_WORK_DIR for portable scripts. Native macOS supports dependencies
        declared as package names or package==version. Existing output overwrites require if_unchanged
        and expected_files. Returns execution and publication statuses.
        """
        in_place = mode in ('workspace_ro', 'workspace_rw')
        publication = {}
        if output is not None:
            if in_place:
                raise ValueError('就地执行不接受 output 发布策略，请省略 output')
            publication['output'] = output
        request = PythonRequest(
            path=path, code=code, args=args or [], mode=mode, inputs=inputs or [],
            working_directory=working_directory,
            code_directory=code_directory, allow_delete=allow_delete, execution_hint=execution_hint,
            environment=environment, timeout_seconds=timeout_seconds, dependencies=dependencies or [],
            **publication,
        )
        if in_place:
            return await run_bound(tool_call_id,request,'run_python')
        from .workspace_execution import RunnerClient, runner_for
        if execution_service and getattr(execution_service,'runners',{}) and isinstance(runner_for(execution_service,ws),RunnerClient):
            return await run_bound(tool_call_id, request, 'run_python')
        return await executor.run_python(tool_call_id, request, interrupt, emit)

    async def run_bound(call_id, request, name):
        from .workspace_execution import workspace_execution
        if execution_service is None:
            raise ValueError('未配置 workspace 执行器')
        return await workspace_execution(execution_service,ws,call_id,request,interrupt,emit,name)

    @tool(args_schema=CommandArguments)
    async def run_command(tool_call_id: Annotated[str, InjectedToolCallId], argv: list[str],
                          mode: str = 'workspace_ro', working_directory: str | None = None,
                          inputs: list[InputResource] | None = None, timeout_seconds: int = 120):
        """Run argv in the bound workspace sandbox after approval. Supports installed rg, git status
        and ruff format. Use workspace_ro to inspect, workspace_rw for immediate changes, or
        isolated with declared inputs at /inputs/name. No shell syntax or interactive stdin."""
        request = CommandRequest(argv=argv,mode=mode,working_directory=working_directory,
                                 inputs=inputs or [],timeout_seconds=timeout_seconds)
        return await run_bound(tool_call_id, request, 'run_command')

    tools = [project_tree, project_list, project_read, project_write, project_edit,
             project_delete, project_move, project_mkdir, project_change_plan,
             project_change_apply, project_authorize, run_python]
    from .workspace_execution import runner_for
    if execution_service is not None and getattr(execution_service,'runners',{}):
        bound = runner_for(execution_service,ws)
        if bound:
            if hasattr(bound,'capabilities'):
                available = bound.capabilities()['commands']
                run_command.description += ' Installed commands: ' + ', '.join(available)
                if available:
                    tools.append(run_command)
            else:
                tools.append(run_command)
    if os.getenv('ARTORIAS_PROJECT_ANALYSIS', '1') == '1':
        tools[:0] = [project_inventory, project_search, project_read_many]
    return tools
