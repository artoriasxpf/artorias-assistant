"""Development-only logical cloud space backed by this installation's disk."""
from pathlib import Path


class ManagedCloudConnector:
    def __init__(self, filesystem):
        self.filesystem = filesystem

    def open(self, workspace, context):
        logical = Path(workspace['root'])
        if logical.is_absolute() or '..' in logical.parts:
            raise ValueError('非法云端逻辑路径')
        routed = dict(workspace, root=str(Path(workspace['location_root'])/logical))
        routed['execution_root'] = str(Path(workspace['location_root'])/workspace.get('execution_root',logical))
        return self.filesystem.open(routed, context)
