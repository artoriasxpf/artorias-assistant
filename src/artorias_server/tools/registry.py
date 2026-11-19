"""DeepAgents tool adapter; business operations and execution backends are injected."""
from langchain_core.tools import tool, InjectedToolCallId
from typing import Annotated
from artorias_shared.execution.models import InputResource, OutputPolicy, PythonRequest

from .files import FileToolService
from .executor import ToolExecutor
from artorias_shared.approvals import ApprovalRequest


class RunPythonArguments(PythonRequest):
    tool_call_id: Annotated[str, InjectedToolCallId]


def build_tools(ws, emit, *, sandbox=None, execution_service=None):
    files = FileToolService(ws)
    sandbox = ToolExecutor(sandbox)

    @tool
    def project_list() -> list[str]:
        """List persistent user project files (not internal agent scratch files)."""
        return ws.files()

    @tool
    def project_read(path: str) -> str:
        """Read a UTF-8 project file by relative path."""
        return files.read(path)

    @tool
    def project_write(path: str, content: str) -> dict:
        """Create or replace a UTF-8 project file with a versioned change."""
        return files.write(path, content)

    @tool
    def project_edit(path: str, old: str, new: str) -> dict:
        """Replace exactly one occurrence of old text in a project file."""
        return files.edit(path, old, new)

    @tool
    def project_delete(path: str) -> str:
        """Delete a project file, preserving its previous content version."""
        return ws.delete(path)

    @tool(args_schema=RunPythonArguments)
    async def run_python(path: str, tool_call_id: Annotated[str, InjectedToolCallId],
                         args: list[str] | None = None, mode: str = 'isolated',
                         inputs: list[InputResource] | None = None, output: OutputPolicy | None = None,
                         working_directory: str | None = None, code_directory: str | None = None,
                         allow_delete: bool = False, execution_hint: str = 'auto',
                         environment: str = 'python-default', timeout_seconds: int = 120) -> dict:
        """Execute a saved Python script after approval. Declare input files/directories; they
        appear in /inputs, code_directory in /app. Write deliverables under /outputs.
        isolated has empty /work; workspace_copy requires working_directory and conditionally
        commits its changes. output.target defaults to the Task workspace artifacts directory.
        Existing output overwrites need overwrite=if_unchanged and expected_files declared before
        execution. No network or automatic dependencies; only the installed python-default profile.
        Returns execution and publication statuses separately; conflicts retain downloadable artifacts.
        """
        from langgraph.types import interrupt
        if execution_service is None:
            # Compatibility for explicitly injected v0.7 adapters; advanced requests cannot downgrade.
            if args or inputs or (output is not None and output != OutputPolicy()) or mode!='isolated' or working_directory or code_directory or allow_delete or execution_hint!='auto':
                raise ValueError('此执行器不支持产物模式')
            decision=interrupt(ApprovalRequest('code_execution',path,{'notice':'Legacy sandbox: logs only'}).to_payload())
            if decision!='approve':return {'rejected':True}
            result=await sandbox.run_python(ws,path)
        else:
            request=dict(path=path,args=args or [],mode=mode,inputs=[i.model_dump() for i in inputs or []],
                         output=(output or OutputPolicy()).model_dump(),working_directory=working_directory,
                         code_directory=code_directory,allow_delete=allow_delete,execution_hint=execution_hint,
                         environment=environment,timeout_seconds=timeout_seconds)
            plan=execution_service.prepare(ws,tool_call_id,request)
            decision=interrupt(execution_service.approval(plan))
            if decision!='approve':return execution_service.reject(plan)
            result=await execution_service.execute(ws,plan,emit)
        emit('tool.result', result)
        if execution_service is not None:
            return dict(result, stdout_tail=result.get('stdout_tail','')[-4000:], stderr_tail=result.get('stderr_tail','')[-4000:],
                        artifacts=result.get('artifacts',[])[:30], published=result.get('published',[])[:30], conflicts=result.get('conflicts',[])[:30],
                        details_ref=f"/api/sessions/{ws.session}/executions/{result['execution_id']}")
        return result

    @tool
    def project_authorize(path: str, access: str = 'write') -> dict:
        """Request approval to access an explicit local absolute directory in this Run."""
        if not hasattr(ws, 'grant_access'):
            raise ValueError('当前工作空间不支持扩展授权')
        if access not in ('read', 'write'):
            raise ValueError('access 必须为 read 或 write')
        from langgraph.types import interrupt
        decision = interrupt(ApprovalRequest('file_access', path, {'access':access,'scope':'当前 Run；包含目录下的文件'}).to_payload())
        if decision != 'approve':
            return {'rejected': True}
        return ws.grant_access(path, access)

    return [project_list, project_read, project_write, project_edit, project_delete, run_python, project_authorize]
