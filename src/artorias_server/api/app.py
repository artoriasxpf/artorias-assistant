import asyncio, contextlib, hashlib, json, os, secrets
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from artorias_server.storage.sqlite import Store
from artorias_server.scheduler.service import Scheduler
from artorias_server.workspace.routing import WorkspaceService
from artorias_server.workspace.catalog import WorkspaceCatalog
from artorias_server.workspace.connectors import ConnectorUnavailable

class Input(BaseModel):
    text:str=Field(min_length=1,max_length=30000)
    request_key:str=Field(min_length=1,max_length=100)

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
    @app.get('/api/sessions/{sid}/events')
    async def events(sid:str,request:Request,after:int=0):
        store.require(sid)
        from artorias_server.sse.events import stream_events
        return StreamingResponse(stream_events(store,sid,request,after),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})
    @app.post('/api/runs/{rid}/cancel')
    def cancel(rid:str):return service.cancel_run(rid)
    @app.post('/api/runs/{rid}/decision')
    def decision(rid:str,body:dict):
        value=body.get('decision')
        if value not in ('approve','reject'):raise ValueError('Invalid decision')
        return service.decide(rid,value,body.get('request_seq'))
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
    def execution(sid:str,eid:str):
        from artorias_server.storage.executions import ExecutionRepository
        store.require(sid)
        row=ExecutionRepository(store).get(eid)
        if not row or row['session_id']!=sid:raise KeyError(eid)
        return {'id':eid,'status':row['status'],'result':row['result']}

    @app.get('/api/sessions/{sid}/executions/{eid}/logs')
    def execution_logs(sid:str,eid:str):
        from artorias_server.storage.executions import ExecutionRepository
        store.require(sid)
        row=ExecutionRepository(store).get(eid)
        if not row or row['session_id']!=sid:raise KeyError(eid)
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
        return catalog.register(body['name'],body['root'],body.get('kind','device_fs'))
    @app.post('/api/sessions/{sid}/workspace')
    def select_workspace(sid:str,body:dict):
        return catalog.select(sid,body['workspace_id'])
    @app.get('/api/workspace-grants')
    def grants():return store.query('SELECT * FROM workspace_grants')
    @app.post('/api/workspace-grants/{gid}/revoke')
    def revoke(gid:str):
        with store.tx() as c:c.execute('DELETE FROM workspace_grants WHERE id=?',(gid,))
        return {'revoked':True}
    return app
