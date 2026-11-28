"""Single-owner runtime with workspace execution delegated to registered runners."""
import asyncio
from contextlib import asynccontextmanager, suppress
from functools import partial
import os
from pathlib import Path

from dotenv import load_dotenv
import artorias_web
from artorias_server.api.app import create_http_app
from artorias_server.runtime.service import DeepAgentRuntime
from artorias_server.scheduler.service import Scheduler
from artorias_server.storage.sqlite import Store
from artorias_server.tasks.executor import TaskRuntime
from artorias_server.tools.execution_service import ExecutionService
from artorias_server.tools.registry import build_tools
from artorias_server.workspace.factory import WorkspaceFactory
from artorias_server.workspace.routing import WorkspaceService


def create_app(*, workspace_registry=None):
    load_dotenv()
    data = Path(os.getenv('ARTORIAS_DATA','data')).resolve()
    data.mkdir(parents=True,exist_ok=True)
    store = Store(data/'app.sqlite')
    service = ExecutionService(data,store)
    if not service.runners:
        raise ValueError('远程 runtime 需要配置 ARTORIAS_RUNNER_ROUTES')
    engine = DeepAgentRuntime(data,store,workspace_factory=WorkspaceFactory(store,workspace_registry),
                              tool_factory=partial(build_tools,execution_service=service))
    tasks = TaskRuntime(data,store,engine=engine)
    scheduler = Scheduler(store,tasks)

    @asynccontextmanager
    async def lifespan(app):
        await service.recover(lambda ctx:WorkspaceService(store,ctx['session'],ctx['run'],ctx['task'],
                                                          registry=workspace_registry))
        workers = [asyncio.create_task(scheduler.loop())]
        index = tasks.repo.search.index
        if getattr(index,'remote',False):
            from artorias_server.integrations.search.worker import SearchSyncWorker
            workers.append(asyncio.create_task(SearchSyncWorker(tasks.repo.search).run()))
        try:
            yield
        finally:
            for worker in workers:
                worker.cancel()
            for worker in workers:
                with suppress(asyncio.CancelledError):
                    await worker
            if getattr(index,'remote',False):
                index.client.close()

    return create_http_app(data,store,scheduler,workspace_registry=workspace_registry,
                           web_root=Path(artorias_web.__file__).parent,lifespan=lifespan)
