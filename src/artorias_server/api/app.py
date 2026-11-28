import asyncio, contextlib, hashlib, json, os, secrets
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse, PlainTextResponse, Response
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from artorias_server.storage.sqlite import Store
from artorias_server.scheduler.service import Scheduler
from artorias_server.tasks.reviews import Decision, decide as decide_review
from artorias_server.workspace.routing import WorkspaceService
from artorias_server.workspace.catalog import WorkspaceCatalog
from artorias_server.workspace.connectors import ConnectorUnavailable

class Input(BaseModel):
    text:str=Field(min_length=1,max_length=30000)
    request_key:str=Field(min_length=1,max_length=100)

class WorkResume(BaseModel):
    request_key: str = Field(min_length=1, max_length=100)
    generation: int = Field(ge=1)
    additional_calls: int = Field(default=0, ge=0, le=1000)


def web_revision(web_root):
    """Deterministic signature over the served frontend files, for live reload."""
    root = Path(web_root)
    entries = []
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.'))
        for name in files:
            if name.startswith('.'):
                continue
            path = Path(directory) / name
            try:
                entries.append((path.relative_to(root).as_posix(), path.stat().st_mtime_ns))
            except OSError:
                continue
    return hashlib.sha1(repr(sorted(entries)).encode('utf-8')).hexdigest()

def create_http_app(data,store,scheduler=None, *, workspace_registry=None, web_root=None, lifespan=None, folder_picker=None):
    from artorias_server.workspace.connectors import ConnectorRegistry
    registry = workspace_registry if workspace_registry is not None else ConnectorRegistry()
    token=secrets.token_urlsafe(32)
    from artorias_server.application.service import AssistantService
    service=AssistantService(store,scheduler)
    catalog=WorkspaceCatalog(store)
    app=FastAPI(title='Artorias Assistant',lifespan=lifespan)
    if web_root is not None:
        app.mount('/static', StaticFiles(directory=web_root), name='static')
        @app.get('/dev/web-revision')
        def web_revision_endpoint():
            return JSONResponse({'revision': web_revision(web_root)}, headers={'Cache-Control': 'no-store'})
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=['localhost','127.0.0.1','testserver'])
    @app.middleware('http')
    async def protect(request,call_next):
        if request.url.path.startswith('/api/'):
            if request.headers.get('x-artorias-token')!=token:return JSONResponse({'detail':'Unauthorized'},401)
            origin=request.headers.get('origin')
            if origin and origin!=str(request.base_url).rstrip('/'):return JSONResponse({'detail':'Origin rejected'},403)
        try:return await call_next(request)
        except PermissionError as e:return JSONResponse({'detail':str(e)},403)
        except ConnectorUnavailable as e:return JSONResponse({'detail':str(e)},503)
        except FileNotFoundError:return JSONResponse({'detail':'File not found'},404)
        except KeyError:return JSONResponse({'detail':'Not found'},404)
        except ValueError as e:return JSONResponse({'detail':str(e)},409)
    if web_root is not None:
        @app.get('/')
        def index():return FileResponse(Path(web_root)/'index.html')
    @app.get('/bootstrap')
    def bootstrap():
        return JSONResponse({'token':token,'native_folder_picker':folder_picker is not None},
                            headers={'Cache-Control':'no-store'})
    @app.get('/api/sessions')
    def sessions():
        return store.query("""
            SELECT s.*, (SELECT substr(m.content, 1, 80) FROM messages m
                          WHERE m.session=s.id AND m.role='user' ORDER BY m.rowid LIMIT 1) AS title
            FROM sessions s ORDER BY s.rowid DESC
        """)
    @app.post('/api/sessions')
    def new_session():
        result=service.create_session();catalog.current(result['id']);return result
    @app.post('/api/sessions/{sid}/messages')
    def submit(sid:str,body:Input):
        result=service.submit_message(sid,body.text,body.request_key)
        catalog.snapshot_run(result['id'],sid)
        return result
    @app.get('/api/sessions/{sid}/work-plans')
    def waiting_work(sid:str):
        store.require(sid)
        return store.query('''SELECT p.id,p.run,p.step_key,p.task_id,p.reason,p.generation,p.calls,p.call_limit,
            b.calls AS task_calls,b.call_limit AS task_limit FROM work_plans p
            JOIN work_task_budgets b ON p.task_id=b.task_id WHERE p.session=? AND p.status='waiting'
            ORDER BY p.rowid''',(sid,))

    @app.post('/api/sessions/{sid}/work-plans/{pid}/resume')
    def resume_work(sid:str,pid:str,body:WorkResume):
        from artorias_server.work.repository import resume
        workspace = catalog.current(sid)
        text = f'追加 {body.additional_calls} 次模型调用并继续' if body.additional_calls else '继续'
        with store.tx() as db:
            return resume(db,sid,text,body.request_key,workspace['id'],pid,body.generation,body.additional_calls)

    @app.get('/api/sessions/{sid}/events')
    async def events(sid:str,request:Request,after:int=0):
        store.require(sid)
        from artorias_server.sse.events import stream_events
        return StreamingResponse(stream_events(store,sid,request,after),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})
    @app.post('/api/runs/{rid}/cancel')
    def cancel(rid:str):return service.cancel_run(rid)
    @app.post('/api/runs/{rid}/decision')
    def decision(rid:str,body:dict):
        value=body.get('decisions', body.get('decision'))
        return service.decide(rid,value,body.get('request_seq'))
    @app.get('/api/sessions/{sid}/reviews')
    def reviews(sid:str):
        store.require(sid)
        with store.tx() as c:
            rows=c.execute("""SELECT r.id,r.run,r.question,a.criterion_version,a.evidence_version,a.evidence_ref,
                substr(s.result,1,2000) AS preview FROM task_review_requests r
                JOIN task_criterion_assessments a ON a.id=r.assessment_id
                JOIN task_steps s ON s.run=r.run AND s.key=r.step_key
                WHERE r.session=? AND r.state='pending' ORDER BY r.rowid""",(sid,))
            return {'items':[dict(row) for row in rows],
                    'cursor':c.execute('SELECT coalesce(max(seq),0) FROM events WHERE session=?',(sid,)).fetchone()[0]}

    @app.get('/api/sessions/{sid}/reviews/{rid}/evidence')
    def review_evidence(sid:str,rid:str):
        rows=store.query("""SELECT a.evidence_ref,a.evidence_version,s.result,s.result_json
            FROM task_review_requests r JOIN task_criterion_assessments a ON a.id=r.assessment_id
            JOIN task_steps s ON s.run=r.run AND s.key=r.step_key WHERE r.session=? AND r.id=?""",(sid,rid))
        if not rows:
            raise KeyError(rid)
        row=rows[0]
        aid=row['evidence_ref']
        if aid=='response':
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse(row['result'],headers={'X-Content-Type-Options':'nosniff'})
        evidence=json.loads(row['result_json'])['verified_deliveries'][aid]
        if evidence.get('execution_id'):
            return artifact(sid,evidence['execution_id'],aid)
        changes=store.query("""SELECT r.blob_root FROM resource_changes r JOIN changes c ON c.id=r.change_id
            WHERE c.session=? AND c.id=? AND c.after_hash=?""",(sid,aid,row['evidence_version']))
        if not changes:
            raise KeyError(aid)
        return FileResponse(Path(changes[0]['blob_root'])/row['evidence_version'],
                            filename=Path(evidence['path']).name,headers={'X-Content-Type-Options':'nosniff'})

    @app.post('/api/sessions/{sid}/reviews/{rid}/decision')
    def review_decision(sid:str,rid:str,body:Decision):
        runtime = getattr(scheduler, 'runtime', None)
        if not hasattr(runtime, 'repo'):
            raise ConnectorUnavailable('当前部署未配置 Task 验收服务')
        return decide_review(store,runtime.repo.search,sid,rid,body)

    @app.get('/api/sessions/{sid}/workspace')
    def workspace(sid:str):
        store.require(sid)
        return WorkspaceService(store,sid,registry=registry).describe()
    @app.get('/api/sessions/{sid}/files')
    def files(sid:str):
        store.require(sid);ws=WorkspaceService(store,sid,registry=registry)
        result=[]
        for p in ws.files():
            try:version=ws.head(p)
            except ValueError:version=None
            result.append({'path':p,'version':version})
        return result
    @app.get('/api/sessions/{sid}/file')
    def file(sid:str,path:str):
        store.require(sid);return {'content':WorkspaceService(store,sid,registry=registry).read(path)}
    @app.get('/api/sessions/{sid}/changes')
    def changes(sid:str):store.require(sid);return registry.list_changes(sid)
    @app.post('/api/sessions/{sid}/changes/{cid}/revert')
    def revert(sid:str,cid:str):
        store.require(sid);return {'id':WorkspaceService(store,sid,registry=registry).revert(cid)}
    @app.get('/api/sessions/{sid}/executions/{eid}')
    async def execution(sid:str,eid:str):
        from artorias_server.storage.executions import ExecutionRepository
        store.require(sid)
        row=ExecutionRepository(store).get(eid)
        if not row or row['session_id']!=sid:raise KeyError(eid)
        if row['status']=='running' and 'runner' in row['plan']:
            from artorias_server.tools.workspace_execution import configured_runners
            import httpx
            plan = row['plan']
            client = configured_runners().get((plan['kind'],plan['location_id']))
            if client:
                try:
                    result = await client.status(eid)
                except (httpx.HTTPError,ValueError):
                    return {'id':eid,'status':'unknown','result':{'error':'暂时无法联系执行器'}}
                if result['execution_status'] not in ('running','prepared','unknown'):
                    ExecutionRepository(store).save(eid,'completed',result)
                    return {'id':eid,'status':'completed','result':result}
                return {'id':eid,'status':result['execution_status'],'result':result}
        return {'id':eid,'status':row['status'],'result':row['result']}

    def runner_file(row, resource):
        from artorias_server.tools.workspace_execution import configured_runners
        plan = row['plan']
        runner = configured_runners().get((plan['kind'],plan['location_id']))
        if not runner:
            raise ConnectorUnavailable('执行器未连接')
        return runner.fetch(row['id'],resource)

    @app.get('/api/sessions/{sid}/executions/{eid}/logs')
    def execution_logs(sid:str,eid:str):
        from artorias_server.storage.executions import ExecutionRepository
        store.require(sid)
        row=ExecutionRepository(store).get(eid)
        if not row or row['session_id']!=sid:raise KeyError(eid)
        if 'runner' in row['plan']:
            result = row['result'] or {}
            return PlainTextResponse('STDOUT\n'+result.get('stdout_tail','')+'\nSTDERR\n'+result.get('stderr_tail',''))
        path=Path(data)/'executions'/row['id']/'logs.txt'
        if not path.is_file():raise FileNotFoundError(eid)
        return FileResponse(path,filename='python-logs.txt',media_type='text/plain',headers={'X-Content-Type-Options':'nosniff'})

    @app.get('/api/sessions/{sid}/executions/{eid}/artifacts/{aid}')
    def artifact(sid:str,eid:str,aid:str):
        from artorias_server.storage.executions import ExecutionRepository
        store.require(sid)
        row=ExecutionRepository(store).get(eid)
        if not row or row['session_id']!=sid:raise KeyError(eid)
        items=(row['result'] or {}).get('artifacts',[])
        item=next((x for x in items if x['id']==aid and x['action']=='write'),None)
        if item is None:raise KeyError(aid)
        if 'runner' in row['plan']:
            content = runner_file(row, 'artifacts/'+item['id'])
            if hashlib.sha256(content).hexdigest() != item['sha256']:
                raise ValueError('远程产物校验失败')
            return Response(content,media_type='application/octet-stream',
                            headers={'X-Content-Type-Options':'nosniff','Content-Disposition':'attachment'})
        path=Path(data)/'executions'/row['id']/'artifacts'/item['id']
        return FileResponse(path,filename=Path(item['path']).name,media_type='application/octet-stream',headers={'X-Content-Type-Options':'nosniff'})

    @app.get('/api/workspaces')
    def list_workspaces():
        return store.query('SELECT w.*,l.kind FROM workspaces w JOIN workspace_locations l ON l.id=w.location_id')
    if folder_picker is not None:
        @app.post('/api/workspaces/pick-folder')
        def pick_workspace_folder():
            return {'root': folder_picker()}
    @app.post('/api/workspaces')
    def register_workspace(body:dict):
        # This authenticated local UI action is the owner's persistent grant.
        return catalog.register(body['name'],body['root'],body.get('kind','device_fs'),
                                persistent=body.get('persistent', True) is True)
    @app.post('/api/sessions/{sid}/workspace')
    def select_workspace(sid:str,body:dict):
        return catalog.select(sid,body['workspace_id'])
    from artorias_server.permissions import Permissions
    Permissions(store)

    @app.post('/api/sessions/{sid}/permissions/workspace')
    def allow_session_workspace(sid: str, body: dict):
        from artorias_shared.ids import uid

        store.require(sid)
        workspace = catalog.get(body['workspace_id'])
        access = body['access']
        if access not in ('read', 'write'):
            raise ValueError('access 必须为 read 或 write')
        with store.tx() as db:
            existing = db.execute(
                """SELECT id FROM permission_grants WHERE session_id=? AND workspace_id=?
                   AND resource_kind='directory' AND root=? AND action=? AND revoked_at IS NULL""",
                (sid, workspace['id'], workspace['root'], access),
            ).fetchone()
            if existing:
                return {'id': existing['id']}
            grant_id = uid()
            db.execute(
                """INSERT INTO permission_grants
                   (id,session_id,workspace_id,location_id,action,resource_kind,root)
                   VALUES (?,?,?,?,?,'directory',?)""",
                (grant_id, sid, workspace['id'], workspace['location_id'], access, workspace['root']),
            )
        return {'id': grant_id}

    @app.get('/api/sessions/{sid}/permissions')
    def session_permissions(sid: str):
        store.require(sid)
        return store.query(
            'SELECT * FROM permission_grants WHERE session_id=? AND revoked_at IS NULL ORDER BY created_at',
            (sid,),
        )

    @app.post('/api/sessions/{sid}/permissions/{gid}/revoke')
    def revoke_session_permission(sid: str, gid: str):
        store.require(sid)
        with store.tx() as db:
            updated = db.execute(
                """UPDATE permission_grants SET revoked_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                   WHERE id=? AND session_id=? AND revoked_at IS NULL""",
                (gid, sid),
            )
        return {'revoked': bool(updated.rowcount)}

    @app.get('/api/workspace-grants')
    def grants():return store.query('SELECT * FROM workspace_grants')
    @app.post('/api/workspace-grants/{gid}/revoke')
    def revoke(gid:str):
        with store.tx() as c:c.execute('DELETE FROM workspace_grants WHERE id=?',(gid,))
        return {'revoked':True}
    return app
