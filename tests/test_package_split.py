"""Regression tests for deployment boundaries introduced in v0.7."""
import ast
import asyncio
from pathlib import Path
import subprocess
import sys
import pytest
from artorias_shared.workspace import WorkspaceContext, ConnectorUnavailable
from artorias_device.storage.journal import SQLiteWorkspaceJournal
from artorias_device.workspace.connector import LocalFilesystemConnector
from artorias_server.workspace.connectors.remote_device import RemoteDeviceConnector
from artorias_server.workspace.connectors.cloud import CloudStorageConnector
from artorias_server.tools.executor import ExecutorRegistry, ToolExecutor
from artorias_shared.tools import ToolRequest


def test_dependency_direction():
    root = Path(__file__).resolve().parents[1]/'src'
    forbidden = {
        'artorias_search': ('artorias_server', 'artorias_device', 'artorias_local_app', 'fastapi', 'langchain', 'deepagents'),
        'artorias_search_service': ('artorias_server', 'artorias_device', 'artorias_local_app', 'langchain', 'deepagents'),
        'artorias_search_client': ('artorias_server', 'artorias_device', 'artorias_local_app', 'jieba', 'fastapi', 'langchain', 'deepagents'),
        'artorias_shared': ('artorias_server', 'artorias_device', 'artorias_local_app', 'fastapi', 'langchain', 'deepagents'),
        'artorias_device': ('artorias_server', 'artorias_local_app', 'fastapi', 'langchain', 'deepagents'),
        'artorias_server': ('artorias_device', 'artorias_local_app'),
    }
    for package, prefixes in forbidden.items():
        for path in (root/package).rglob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ''] if isinstance(node, ast.ImportFrom) and not node.level else []
                assert not any(name.startswith(prefixes) for name in names), path


def test_device_runs_without_server_or_llm_imports(tmp_path):
    code = r"""
import sys
from pathlib import Path
class Block:
    def find_spec(self, fullname, *args):
        if fullname.startswith(('artorias_server', 'deepagents', 'langchain', 'fastapi')):
            raise ImportError(fullname)
sys.meta_path.insert(0, Block())
from artorias_device.storage.journal import SQLiteWorkspaceJournal
from artorias_device.workspace.local_workspace import LocalWorkspace
root=Path(sys.argv[1]); journal=SQLiteWorkspaceJournal(root/'data/device.sqlite')
ws=LocalWorkspace(root/'data/blobs',journal,'s','r',workspace_root=root/'project')
a=ws.write('a.txt','before',expected=None)
ws.write('a.txt','after',expected=a['version'])
change=journal.list_changes('s')[0]['id']
ws.revert(change)
assert ws.read('a.txt')=='before'
assert len(journal.list_changes('s'))==3
"""
    subprocess.run([sys.executable, '-c', code, str(tmp_path)], check=True)


def test_server_never_falls_back_to_device(tmp_path):
    from artorias_server.storage.sqlite import Store
    from artorias_server.workspace.routing import WorkspaceService
    db=Store(tmp_path/'app.sqlite'); ws=WorkspaceService(db,db.session())
    with pytest.raises(ConnectorUnavailable):
        ws.write('a.txt','no fallback',expected=None)
    assert not (Path(ws.root)/'a.txt').exists()


@pytest.mark.parametrize('kind,adapter', [('device_fs',RemoteDeviceConnector),('cloud_storage',CloudStorageConnector)])
def test_connector_uses_injected_client_and_validates_location(kind,adapter):
    calls=[]; proxy=object()
    class Client:
        def open_workspace(self, workspace, context):
            calls.append((workspace,context));return proxy
    connector=adapter('location-1',Client())
    workspace={'kind':kind,'location_id':'location-1','root':'project'}
    assert connector.open(workspace,WorkspaceContext('s','r','t')) is proxy
    assert calls == [(workspace,{'session_id':'s','run_id':'r','task_id':'t'})]
    with pytest.raises(ValueError):
        connector.open(dict(workspace,location_id='wrong'),WorkspaceContext('s'))


def test_executor_registry_does_not_infer_location():
    class Executor:
        async def run_python(self, workspace, path):
            return {'path':path,'workspace':workspace}
    registry=ExecutorRegistry();registry.register('remote',Executor())
    executor=ToolExecutor(registry=registry,default_executor='remote')
    result=asyncio.run(executor.execute(ToolRequest('run_python',{'path':'a.py'}),'cloud-space'))
    assert result.payload=={'path':'a.py','workspace':'cloud-space'}
    with pytest.raises(ConnectorUnavailable):
        asyncio.run(executor.execute(ToolRequest('run_python',{'path':'a.py'},'missing'),'cloud-space'))


def test_api_does_not_start_an_injected_worker_implicitly(tmp_path):
    from fastapi.testclient import TestClient
    from artorias_server.storage.sqlite import Store
    from artorias_server.api.app import create_http_app
    class Scheduler:
        async def loop(self):
            raise AssertionError('API must not launch a worker')
    db=Store(tmp_path/'app.sqlite')
    with TestClient(create_http_app(tmp_path,db,Scheduler())) as client:
        assert client.get('/bootstrap').status_code==200
