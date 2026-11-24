"""Deployment-owned executors; workspace routing stays with file operations."""
from pathlib import PurePosixPath

from artorias_shared.approvals import ApprovalRequest
from artorias_shared.execution.models import OutputPolicy, PythonRequest
from artorias_shared.ports.execution import ProjectWorkspacePort
from artorias_shared.tools import ToolRequest, ToolResult
from artorias_shared.workspace import ConnectorUnavailable
from .files import FileToolService

class ExecutorRegistry:
    def __init__(self):
        self.executors = {}

    def register(self, name, executor):
        self.executors[name] = executor

    def get(self, name):
        if name not in self.executors:
            raise ConnectorUnavailable(f'未配置执行器：{name}')
        return self.executors[name]

class ToolExecutor:
    def __init__(self, sandbox=None, *, registry=None, default_executor='default'):
        self.registry = registry if registry is not None else ExecutorRegistry()
        self.default_executor = default_executor
        if sandbox is not None:
            self.registry.register(default_executor, sandbox)

    async def execute(self, request: ToolRequest, workspace):
        if request.name != 'run_python':
            raise ValueError('Unsupported executor operation')
        adapter = self.registry.get(request.executor or self.default_executor)
        result = await adapter.run_python(workspace, request.arguments['path'])
        return ToolResult(status='completed', payload=result)

    async def run_python(self, workspace, path):
        result = await self.execute(ToolRequest('run_python', {'path': path}), workspace)
        return result.payload


class WorkspaceToolExecutor:
    """Runtime tool boundary. Paths go to workspace connectors; Python uses execution adapters."""

    def __init__(self, workspace: ProjectWorkspacePort, *, execution_service=None, sandbox=None):
        self.workspace = workspace
        self.files = FileToolService(workspace)
        self.execution_service = execution_service
        self.sandbox = ToolExecutor(sandbox)

    def list_directory(self, path):
        return {'path': path, 'entries': self.workspace.list_directory(path)}

    def tree(self, path, *, max_depth, max_entries, include_hidden):
        if max_depth < 0 or not 1 <= max_entries <= 1000:
            raise ValueError('目录树深度必须非负，max_entries 必须在 1 到 1000 之间')
        return {'path': path, **self.workspace.tree(
            path, max_depth=max_depth, max_entries=max_entries, include_hidden=include_hidden,
        )}

    def read(self, path, offset, limit, expected_version):
        return self.files.read_page(path, offset, limit, expected_version)

    def write(self, path, content):
        return self.files.write(path, content)

    def edit(self, path, old, new):
        return self.files.edit(path, old, new)

    def delete(self, path):
        return self.workspace.delete(path)

    def authorize(self, path, access, approve):
        if not hasattr(self.workspace, 'grant_access'):
            raise ConnectorUnavailable('当前工作空间不支持本机目录授权')
        target = PurePosixPath(path)
        if not target.is_absolute() or '..' in target.parts or '\x00' in path or '\\' in path:
            raise ValueError('请提供本机绝对目录路径')
        if access not in ('read', 'write'):
            raise ValueError('access 必须为 read 或 write')
        request = ApprovalRequest('file_access', path, {'access':access, 'scope':'当前 Run；包含目录下的文件'})
        if approve(request.to_payload()) != 'approve':
            return {'rejected': True}
        return self.workspace.grant_access(path, access)

    async def run_python(self, call_id, request: PythonRequest, approve, emit):
        service = self.execution_service
        if service is None:
            if (request.args or request.inputs or request.output != OutputPolicy()
                    or request.mode != 'isolated' or request.working_directory or request.code_directory
                    or request.dependencies or request.allow_delete or request.execution_hint != 'auto'):
                raise ValueError('此执行器不支持产物模式')
            approval = ApprovalRequest('code_execution', request.path, {'notice':'Legacy sandbox: logs only'})
            if approve(approval.to_payload()) != 'approve':
                return {'rejected': True}
            result = await self.sandbox.run_python(self.workspace, request.path)
        else:
            plan = service.prepare(self.workspace, call_id, request)
            if approve(service.approval(plan)) != 'approve':
                return service.reject(plan)
            result = await service.execute(self.workspace, plan)
        emit('tool.result', {**result, 'call_id':call_id, 'name':'run_python'})
        if service is None:
            return result
        return dict(result, stdout_tail=result.get('stdout_tail','')[-4000:], stderr_tail=result.get('stderr_tail','')[-4000:],
                    artifacts=result.get('artifacts',[])[:30], published=result.get('published',[])[:30],
                    conflicts=result.get('conflicts',[])[:30],
                    details_ref=f"/api/sessions/{self.workspace.session}/executions/{result['execution_id']}")
