"""In-process device adapter. Its journal is owned by this endpoint."""
from pathlib import Path
from artorias_shared.execution.workspace import digest
from artorias_shared.workspace import WorkspaceContext, WorkspaceJournal
from .local_workspace import LocalWorkspace


class LocalFilesystemConnector:
    def __init__(self, journal: WorkspaceJournal, *, runner_data=None):
        self.journal = journal
        self.runner_data = runner_data

    def open(self, workspace: dict, context: WorkspaceContext):
        execution_gate = None
        if self.runner_data is not None:
            root = Path(workspace.get('execution_root',workspace['root'])).resolve()
            execution_gate = self.runner_data / ('lock-' + digest(str(root)))
        return LocalWorkspace(workspace['blob_root'], self.journal, context.session_id, context.run_id,
            workspace_root=workspace['root'], bind_session=False, execution_gate=execution_gate, create_root=workspace.get('create_root',True),
            resource_meta=dict(backend=workspace['kind'], location_id=workspace['location_id'],
                               task_id=context.task_id, workspace_id=workspace.get('id')))
