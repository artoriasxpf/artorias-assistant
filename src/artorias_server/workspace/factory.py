"""Workspace factory; connector selection is injected by the composition root."""
from .routing import WorkspaceService

class WorkspaceFactory:
    def __init__(self, store, registry=None):
        self.store, self.registry = store, registry

    def __call__(self, run):
        return WorkspaceService(self.store, run['session'], run['id'], run.get('task_id'), registry=self.registry)
