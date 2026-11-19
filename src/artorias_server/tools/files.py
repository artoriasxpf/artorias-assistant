"""File operations shared by tool adapters; no model-framework dependency."""
from artorias_shared.ports.execution import WorkspacePort

class FileToolService:
    def __init__(self, workspace: WorkspacePort):
        self.workspace = workspace
        # Execution-local baseline. After recreation callers must read again.
        self.observed_versions = {}

    def read(self, path):
        text, version = self.workspace.read_version(path)
        self.observed_versions[self.workspace.path(path)] = version
        return text

    def write(self, path, content):
        key = self.workspace.path(path)
        if key not in self.observed_versions and self.workspace.head(path) is not None:
            raise ValueError('覆盖已有文件前必须先 project_read')
        result = self.workspace.write(path, content, expected=self.observed_versions.get(key))
        self.observed_versions[key] = result['version']
        return result

    def edit(self, path, old, new):
        text, version = self.workspace.read_version(path)
        if not old or text.count(old) != 1:
            raise ValueError('old 必须唯一匹配')
        result = self.workspace.write(path, text.replace(old, new, 1), expected=version)
        self.observed_versions[self.workspace.path(path)] = result['version']
        return result
