"""Cloud backend client adapter; no implicit local-directory fallback."""
from dataclasses import asdict


class CloudStorageConnector:
    def __init__(self, location_id, client):
        self.location_id, self.client = location_id, client

    def open(self, workspace, context):
        if workspace['kind'] != 'cloud_storage' or workspace['location_id'] != self.location_id:
            raise ValueError('Cloud location mismatch')
        return self.client.open_workspace(dict(workspace), asdict(context))
