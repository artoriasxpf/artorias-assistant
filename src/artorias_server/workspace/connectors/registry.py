"""Explicit registration only; importing the server never registers a device."""
from artorias_shared.workspace import ConnectorUnavailable


class ConnectorRegistry:
    def __init__(self, *, changes=None):
        self.adapters = {}
        self.changes = changes

    def register(self, kind, location_id, connector):
        self.adapters[kind, location_id] = connector

    def get(self, kind, location_id):
        try:
            return self.adapters[kind, location_id]
        except KeyError:
            raise ConnectorUnavailable('目标设备或存储连接器不可用；不会改存其他空间') from None

    def find_change(self, change_id, session_id):
        if self.changes is None:
            raise ConnectorUnavailable('未配置变更记录查询适配器')
        return self.changes.find_change(change_id, session_id)

    def list_changes(self, session_id):
        if self.changes is None:
            raise ConnectorUnavailable('未配置变更记录查询适配器')
        return self.changes.list_changes(session_id)
