"""Authenticated workspace-owner endpoint. Start behind TLS or an SSH tunnel."""
import hmac
import json
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from artorias_device.tools.workspace_runner import WorkspaceRunner
from artorias_shared.execution.workspace import WorkspaceJob


class ExecutionRef(BaseModel):
    execution_id: str = Field(pattern=r'^exec_[a-f0-9]{40}$')


class ApprovedRef(ExecutionRef):
    plan_hash: str = Field(pattern=r'^[a-f0-9]{64}$')


def create_runner_app(runner, token):
    if len(token) < 32:
        raise ValueError('Runner token 至少需要 32 个字符')
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware('http')
    async def protect(request: Request, call_next):
        if not hmac.compare_digest(request.headers.get('authorization','').encode(), ('Bearer '+token).encode()):
            return JSONResponse({'detail':'unauthorized'},401)
        try:
            return await call_next(request)
        except (KeyError, ValueError, OSError) as exc:
            return JSONResponse({'detail':str(exc)},409)

    @app.get('/capabilities')
    def capabilities():
        return runner.capabilities()

    @app.get('/executions/{eid}/logs')
    async def logs(eid: str):
        result = await runner.status(eid)
        return PlainTextResponse('STDOUT\n'+result.get('stdout_tail','')+'\nSTDERR\n'+result.get('stderr_tail',''))

    @app.get('/executions/{eid}/artifacts/{aid}')
    async def artifact(eid: str, aid: str):
        _,raw,_,_ = runner.row(eid)
        staged = json.loads(raw).get('staged_id')
        result = await runner.status(eid)
        if not staged or not any(item['id']==aid and item['action']=='write' for item in result.get('artifacts',[])):
            raise KeyError(aid)
        return FileResponse(runner.staging.execution.root/staged/'artifacts'/aid)

    @app.post('/prepare')
    async def prepare(job: WorkspaceJob):
        return await runner.prepare(job)

    @app.post('/start')
    async def start(body: ApprovedRef):
        return await runner.start(body.execution_id, body.plan_hash)

    @app.post('/status')
    async def status(body: ExecutionRef):
        return await runner.status(body.execution_id)

    @app.post('/cancel')
    async def cancel(body: ExecutionRef):
        return await runner.cancel(body.execution_id)

    return app


def create_app():
    with open(os.environ['ARTORIAS_RUNNER_CONFIG']) as handle:
        config = json.load(handle)
    def resolve(job):
        if (job.kind,job.location_id) != (config['kind'],config['location_id']):
            raise ValueError('执行位置不匹配')
        entry = config['workspaces'][job.workspace_id]
        if isinstance(entry,dict):
            access = entry['access']
            if access not in ('read','write'):
                raise ValueError('未知的 workspace 权限')
            req = job.request
            publish = getattr(req,'output',None)
            if access == 'read' and (req.mode in ('workspace_rw','workspace_copy') or
                    req.mode=='isolated' and publish and publish.publish=='auto_if_authorized'):
                raise PermissionError('设备仅授权读取此 workspace')
            entry = entry['root']
        root = Path(entry)
        if not root.is_absolute() or root.resolve() != root:
            raise ValueError('Runner 配置需要规范的绝对路径')
        return root
    from .runner_staging import RunnerStaging
    data = Path(config['data']).resolve()
    return create_runner_app(WorkspaceRunner(data,resolve,staging=RunnerStaging(data/'staging')),
                             os.environ[config['token_env']])
