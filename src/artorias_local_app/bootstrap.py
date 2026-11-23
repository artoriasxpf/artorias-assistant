"""Local composition root: the only package wiring server and device together."""
import asyncio
import contextlib
import os
import sys
from pathlib import Path
from contextlib import asynccontextmanager
from functools import partial
from dotenv import load_dotenv
from artorias_server.storage.sqlite import Store
from artorias_server.scheduler.service import Scheduler
from artorias_server.api.app import create_http_app
from artorias_server.workspace.catalog import WorkspaceCatalog
from artorias_server.workspace.factory import WorkspaceFactory
from artorias_server.workspace.routing import WorkspaceService
from artorias_server.workspace.connectors import ConnectorRegistry
from artorias_device.storage.journal import SQLiteWorkspaceJournal
from artorias_device.workspace.connector import LocalFilesystemConnector
from .managed_workspace import ManagedCloudConnector


def build_registry(store):
    catalog = WorkspaceCatalog(store)
    journal = SQLiteWorkspaceJournal(store.path)
    registry = ConnectorRegistry(changes=journal)
    local = LocalFilesystemConnector(journal)
    registry.register('device_fs', catalog.device_id, local)
    registry.register('cloud_storage', 'cloud-default', ManagedCloudConnector(local))
    return registry


def create_workspace(store, session, run=None, task=None, *, registry=None):
    return WorkspaceService(store, session, run, task, registry=registry or build_registry(store))


def create_engine(data, store, *, cloud_python_backend=None, **kwargs):
    from artorias_server.runtime.service import DeepAgentRuntime
    from artorias_server.tools.registry import build_tools
    kwargs.setdefault('workspace_factory', WorkspaceFactory(store, build_registry(store)))
    if 'tool_factory' not in kwargs:
        from artorias_device.tools.python_backend import DockerPythonBackend
        from artorias_server.tools.execution_service import ExecutionService
        service=ExecutionService(data,store,DockerPythonBackend(),cloud_backend=cloud_python_backend)
        kwargs['tool_factory']=partial(build_tools,execution_service=service)
    engine=DeepAgentRuntime(Path(data), store, **kwargs)
    engine.execution_service=locals().get('service')
    return engine


def create_app(data=None, runtime_factory=None):
    load_dotenv()
    data = Path(data or os.getenv('ARTORIAS_DATA', 'data')).resolve()
    data.mkdir(parents=True, exist_ok=True)
    store = Store(data/'app.sqlite')
    registry = build_registry(store)
    execution_service = None
    if runtime_factory is None:
        from artorias_search import SQLiteTextIndex
        from artorias_server.tasks.executor import TaskRuntime
        engine = create_engine(data, store)
        execution_service = engine.execution_service
        text_index = SQLiteTextIndex()
        runtime_factory = lambda data, store: TaskRuntime(data, store, engine=engine, text_index=text_index)
    scheduler = Scheduler(store, runtime_factory(data, store))
    @asynccontextmanager
    async def lifespan(app):
        if execution_service is not None:
            await execution_service.recover(lambda ctx:create_workspace(store,ctx['session'],ctx['run'],ctx['task'],registry=registry))
        worker = asyncio.create_task(scheduler.loop())
        try:
            yield
        finally:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
    import artorias_web
    from artorias_device.folder_picker import pick_folder
    return create_http_app(data, store, scheduler, workspace_registry=registry,
        web_root=Path(artorias_web.__file__).parent, lifespan=lifespan,
        folder_picker=pick_folder if sys.platform == 'darwin' else None)
