"""Client adapter boundary; the application must supply an authenticated transport.

No device filesystem is imported here. Device gateway/RPC implementation is a
separate deployment feature, not simulated by reading the server's disk.
"""
from dataclasses import asdict


class RemoteDeviceConnector:
    def __init__(self, device_id, client):
        self.device_id, self.client = device_id, client

    def open(self, workspace, context):
        if workspace['kind'] != 'device_fs' or workspace['location_id'] != self.device_id:
            raise ValueError('Device identity mismatch')
        return self.client.open_workspace(dict(workspace), asdict(context))
