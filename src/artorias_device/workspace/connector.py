"""In-process device adapter. Its journal is owned by this endpoint."""
from artorias_shared.workspace import WorkspaceContext, WorkspaceJournal
from .local_workspace import LocalWorkspace


class LocalFilesystemConnector:
    def __init__(self, journal: WorkspaceJournal):
        self.journal = journal

    def open(self, workspace: dict, context: WorkspaceContext):
        return LocalWorkspace(workspace['blob_root'], self.journal, context.session_id, context.run_id,
            workspace_root=workspace['root'], bind_session=False,
            resource_meta=dict(backend=workspace['kind'], location_id=workspace['location_id'],
                               task_id=context.task_id, workspace_id=workspace.get('id')))
