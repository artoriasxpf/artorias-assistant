"""Run existing snapshot/publication semantics at the workspace owner."""
from pathlib import Path
import sys

from artorias_device.storage.journal import SQLiteWorkspaceJournal
from artorias_device.workspace.local_workspace import LocalWorkspace
from artorias_device.tools.macos_python import MacOSVenvPythonBackend
from artorias_device.tools.python_backend import DockerPythonBackend
from artorias_server.storage.sqlite import Store
from artorias_server.tools.execution_service import ExecutionService
from artorias_shared.execution.models import safe_relative


class StagingWorkspace(LocalWorkspace):
    def describe(self):
        return {'kind':'device_fs'}

    def check_access(self,path,action='read'):
        self.path(path)


class RunnerStaging:
    def __init__(self,data):
        self.data = Path(data)
        self.data.mkdir(parents=True,exist_ok=True)
        store = Store(self.data/'staging.sqlite')
        self.journal = SQLiteWorkspaceJournal(self.data/'journal.sqlite')
        backend = (MacOSVenvPythonBackend(self.data/'venvs') if sys.platform=='darwin'
                   else DockerPythonBackend())
        self.execution = ExecutionService(self.data,store,backend,runners={})

    def workspace(self, job, root):
        ws = StagingWorkspace(self.data/'blobs', self.journal, job.workspace_id, None,
                              workspace_root=root,bind_session=False,create_root=False)
        ws.task = None
        return ws

    def plan(self,job,root):
        req = job.request
        for relative in (req.path,req.output.target,req.code_directory,req.working_directory,
                         *(item.resource for item in req.inputs)):
            if relative not in (None,'.'):
                safe_relative(relative)
        ws = self.workspace(job,root)
        # Location routing has already selected this endpoint. The inner backend is local to it.
        plan = self.execution.prepare(ws,job.execution_id,req.model_copy(update={'execution_hint':'auto'}))
        return ws,plan

    def describe(self,job,root):
        _,plan = self.plan(job,root)
        return dict(backend=plan['profile'], staged_id=plan['id'], staged_hash=plan['plan_hash'],
                    inputs=len(plan['inputs']),input_bytes=plan['input_bytes'],output=plan['request']['output'])

    async def execute(self,job,root):
        ws,plan = self.plan(job,root)
        result = await self.execution.execute(ws,plan)
        return dict(result,effects='published' if result.get('published') else 'none')
