"""Deployment-owned executors; workspace routing stays with file operations."""
from artorias_shared.tools import ToolRequest, ToolResult
from artorias_shared.workspace import ConnectorUnavailable

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
