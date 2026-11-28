"""Route by the bound workspace. No host paths cross the runner transport."""
import asyncio
import json
import os
from pathlib import PurePosixPath
from time import monotonic
from urllib.parse import urlsplit
import httpx

from artorias_shared.execution.models import safe_relative
from artorias_shared.execution.workspace import WorkspaceJob, digest


class RunnerClient:
    def __init__(self, url, token):
        endpoint = urlsplit(url)
        if endpoint.scheme != 'https' and not (endpoint.scheme == 'http' and endpoint.hostname in ('127.0.0.1','::1')):
            raise ValueError('Runner 需要 HTTPS 或回环隧道')
        if not token:
            raise ValueError('Runner token 为空')
        self.url = url.rstrip('/')
        self.headers = {'Authorization': 'Bearer ' + token}

    def fetch(self,eid,resource):
        with httpx.Client(trust_env=False,timeout=30) as client:
            with client.stream('GET',self.url+'/executions/'+eid+'/'+resource,headers=self.headers) as response:
                response.raise_for_status()
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data)>32_000_000:
                        raise ValueError('远程产物超出配额')
                return bytes(data)

    async def rpc(self, method, body):
        async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
            response = await client.post(self.url + '/' + method, headers=self.headers, json=body)
            if response.is_error:
                raise ValueError(f'Runner {method} 失败 ({response.status_code}): {response.text[:500]}')
            return response.json()

    async def prepare(self, job):
        return await self.rpc('prepare',job.model_dump(exclude_unset=True))

    async def start(self, eid, plan_hash):
        return await self.rpc('start',dict(execution_id=eid, plan_hash=plan_hash))

    async def status(self, eid):
        return await self.rpc('status',dict(execution_id=eid))

    async def cancel(self, eid):
        return await self.rpc('cancel',dict(execution_id=eid))


def configured_runners():
    config = os.getenv('ARTORIAS_RUNNER_ROUTES')
    if not config:
        return {}
    with open(config) as handle:
        entries = json.load(handle)
    return {(e['kind'],e['location_id']):RunnerClient(e['url'],os.environ[e['token_env']]) for e in entries}


def runner_for(service, ws):
    binding = ws.describe()
    return service.runners.get((binding['kind'],binding['location_id']))


async def workspace_execution(service, ws, call_id, req, approve, emit, name):
    binding = ws.describe()
    runner = runner_for(service, ws)
    if runner is None:
        raise ValueError('该 workspace 未配置就地执行器')
    def check(relative, action, is_directory):
        if relative != '.':
            relative = safe_relative(relative)
        target = str(PurePosixPath(binding['root']) / relative)
        resource = dict(workspace_id=binding['id'], location_id=binding['location_id'],
                        resource_kind='directory' if is_directory else 'file',root=target,action=action)
        if not ws.permissions.matching(ws.session,resource):
            ws.catalog.authorize(binding['location_id'],target,action,ws.run)

    def authorize():
        # The cloud runtime compares logical grants. Only the owner can resolve
        # symlinks, validate directory identity and apply its local policy.
        check(req.working_directory or '.', 'write' if req.mode in ('workspace_rw','workspace_copy') else 'read',True)
        for item in req.inputs:
            check(item.resource,'read',item.directory)
        if name == 'run_python' and req.mode in ('isolated','workspace_copy'):
            if req.path:
                check(req.path,'read',False)
            if req.code_directory:
                check(req.code_directory,'read',True)
            if req.output.publish == 'auto_if_authorized':
                check(req.output.target or 'artifacts','write',True)
    authorize()
    context = dict(session=ws.session, run=ws.run, task=ws.task, step=getattr(ws,'step',None), call=call_id)
    eid = 'exec_' + digest(context)[:40]
    job = WorkspaceJob(execution_id=eid, workspace_id=binding['id'], location_id=binding['location_id'],
                       kind=binding['kind'], request=req)
    request_hash = digest(job.model_dump(exclude_unset=True))
    repo = service.repo
    row = repo.get(eid)
    if row is None:
        remote = await runner.prepare(job)
        plan = dict(id=eid, context=context, request_hash=request_hash, runner=remote,
                    workspace_id=binding['id'], location_id=binding['location_id'], kind=binding['kind'],
                    request=req.model_dump(exclude_unset=True), tool=name)
        row = repo.install(plan)
    if request_hash != row['request_hash']:
        raise ValueError('同一工具调用参数或绑定位置发生变化')
    remote = row['plan']['runner']
    if row['status'] == 'awaiting_approval':
        approved = approve(dict(kind='code_execution',execution_id=eid, tool=name,
            path=getattr(req,'path',None), code=getattr(req,'code',None), argv=getattr(req,'argv',None),
            inputs=[i.model_dump() for i in req.inputs], dependencies=getattr(req,'dependencies',[]),
            plan_hash=remote['plan_hash'], execution_location=binding['location_id'],
            working_directory=remote['directory'], mode=req.mode, network='none', output=remote.get('output'),
            timeout_seconds=req.timeout_seconds, allow_remember=False,
            notice='直接写入原目录，超时或失败可能保留部分修改' if req.mode=='workspace_rw' else '审批后按产物策略发布' if 'staged_id' in remote else '工作空间只读'))
        if approved != 'approve':
            if repo.transition(eid,'awaiting_approval','rejected'):
                repo.save(eid,'rejected',dict(execution_id=eid,execution_status='rejected',publish_status='not_requested'))
                try:
                    await runner.cancel(eid)
                except (httpx.TransportError, ValueError):
                    pass  # The runtime permanently rejects start; the prepared plan also expires.
        else:
            authorize()
            repo.transition(eid,'awaiting_approval','running')
        row = repo.get(eid)
    if row['result']:
        result = row['result']
    elif row['status'] == 'running':
        try:
            # Reissuing start with the same identity only retrieves an already claimed run.
            deadline = monotonic() + req.timeout_seconds + 35
            started = False
            while True:
                try:
                    if not started:
                        result = await runner.start(eid,remote['plan_hash'])
                        started = True
                    else:
                        result = await runner.status(eid)
                except httpx.TransportError:
                    if monotonic() >= deadline:
                        raise
                    await asyncio.sleep(1)
                    continue
                if result['execution_status'] != 'running':
                    break
                if monotonic() >= deadline:
                    raise ValueError('执行端未确认结束，请按 execution_id 核查')
                await asyncio.sleep(.3)
        except asyncio.CancelledError:
            try:
                result = await asyncio.shield(runner.cancel(eid))
                if result['execution_status'] != 'running':
                    repo.save(eid,result['execution_status'],result)
            finally:
                raise
        except ValueError as exc:
            # A lost response can also precede a validation error. Ask the owner
            # whether it accepted the start before treating the failure as terminal.
            try:
                receipt = await runner.status(eid)
            except (httpx.HTTPError,ValueError):
                receipt = {}
            if receipt.get('execution_status') == 'prepared':
                result = dict(execution_status='failed',error=str(exc),effects='none')
                repo.save(eid,'failed',result)
            else:
                result = dict(execution_status='unknown',error=str(exc),
                              effects='workspace_may_have_changed' if req.mode=='workspace_rw' else 'none')
        except httpx.TransportError as exc:
            # Keep the durable claim for reconciliation after reconnect.
            result = dict(execution_status='unknown',error=str(exc),effects='workspace_may_have_changed' if req.mode=='workspace_rw' else 'none')
        else:
            repo.save(eid,'completed',result)
    else:
        result = dict(execution_status=row['status'])
    result = dict(result, execution_id=eid, execution_location=binding['location_id'])
    result.setdefault('publish_status','not_requested')
    emit('tool.result',dict(result, name=name, call_id=call_id))
    result['logs_ref'] = f"/api/sessions/{ws.session}/executions/{eid}/logs"
    return dict(result,artifacts=result.get('artifacts',[])[:30],published=result.get('published',[])[:30],
                conflicts=result.get('conflicts',[])[:30],stdout_tail=result.get('stdout_tail','')[-4000:],stderr_tail=result.get('stderr_tail','')[-4000:])
