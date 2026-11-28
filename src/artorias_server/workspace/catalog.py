"""Workspace bindings and grants. Single-owner local deployment repository."""
import os
from pathlib import Path
from artorias_shared.ids import uid

class WorkspaceCatalog:
    def __init__(self, store):
        self.store = store
        self.data = Path(store.path).resolve().parent
        with store.tx() as c:
            c.executescript('''
CREATE TABLE IF NOT EXISTS workspace_locations(id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('device_fs','cloud_storage')), root TEXT NOT NULL, blob_root TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workspaces(id TEXT PRIMARY KEY, location_id TEXT NOT NULL, name TEXT NOT NULL, root TEXT NOT NULL, UNIQUE(location_id,root));
CREATE TABLE IF NOT EXISTS session_workspaces(session TEXT PRIMARY KEY, workspace_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS task_workspaces(task TEXT PRIMARY KEY, workspace_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run_workspace_bindings(run TEXT NOT NULL, task TEXT NOT NULL DEFAULT '', workspace_id TEXT NOT NULL, PRIMARY KEY(run,task));
CREATE TABLE IF NOT EXISTS workspace_grants(id TEXT PRIMARY KEY, location_id TEXT NOT NULL, scope_run TEXT NOT NULL DEFAULT '', root TEXT NOT NULL, access TEXT NOT NULL CHECK(access IN ('read','write')));
CREATE TABLE IF NOT EXISTS resource_changes(change_id TEXT PRIMARY KEY, task_id TEXT, workspace_id TEXT, location_id TEXT NOT NULL, backend TEXT NOT NULL, root TEXT NOT NULL, blob_root TEXT NOT NULL);
''')
            # Stable device identity survives process restarts; use explicit configuration for deployments.
            device=c.execute("SELECT id FROM workspace_locations WHERE kind='device_fs' LIMIT 1").fetchone()
            self.device_id=device[0] if device else os.getenv('ARTORIAS_DEVICE_ID') or 'device_'+uid()
            c.execute('INSERT OR IGNORE INTO workspace_locations(id,kind,root,blob_root) VALUES(?,?,?,?)', (self.device_id,'device_fs','/',str(self.data/'blobs')))
            c.execute('INSERT OR IGNORE INTO workspace_locations(id,kind,root,blob_root) VALUES(?,?,?,?)', ('cloud-default','cloud_storage',str(self.data/'cloud/files'),str(self.data/'cloud/blobs')))
            if not c.execute("SELECT 1 FROM workspaces WHERE location_id='cloud-default' AND root='personal'").fetchone():
                c.execute('INSERT INTO workspaces(id,name,location_id,root) VALUES(?,?,?,?)',(uid(),'默认云端空间','cloud-default','personal'))
                c.execute('INSERT INTO workspace_grants(id,location_id,root,access,scope_run) VALUES(?,?,?,?,?)',(uid(),'cloud-default','personal','write',''))
    def register(self, name, root, kind='device_fs', *, _legacy=False, persistent=True):
        if kind not in ('device_fs','cloud_storage'): raise ValueError('未知空间类型')
        if kind=='device_fs':
            root=str(Path(root).expanduser().resolve())
            if not _legacy:self._safe_root(root)
            location=self.device_id
        else:
            root=root.strip('/')
            if not root or '..' in Path(root).parts or '\\' in root: raise ValueError('非法云端逻辑路径')
            location='cloud-default'
        with self.store.tx() as c:
            old=c.execute('SELECT id FROM workspaces WHERE location_id=? AND root=?',(location,root)).fetchone()
            wid=old[0] if old else uid()
            c.execute('INSERT OR IGNORE INTO workspaces(id,name,location_id,root) VALUES(?,?,?,?)',(wid,name,location,root))
            if persistent and not c.execute("SELECT 1 FROM workspace_grants WHERE location_id=? AND root=? AND access='write' AND scope_run=''",(location,root)).fetchone():
                c.execute('INSERT INTO workspace_grants(id,location_id,root,access,scope_run) VALUES(?,?,?,?,?)',(uid(),location,root,'write',''))
        return self.get(wid)
    def _safe_root(self, root):
        p=Path(root)
        if p==self.data or p in self.data.parents or self.data in p.parents:
            raise ValueError('不能授权应用数据目录及其祖先目录')
    def get(self, wid):
        rows=self.store.query('SELECT w.*,l.kind,l.blob_root,l.root AS location_root FROM workspaces w JOIN workspace_locations l ON l.id=w.location_id WHERE w.id=?',(wid,))
        if not rows:raise KeyError(wid)
        return rows[0]
    def current(self, session):
        self.store.require(session)
        rows=self.store.query('SELECT workspace_id FROM session_workspaces WHERE session=?',(session,))
        if rows:return self.get(rows[0]['workspace_id'])
        # Import v0.4 binding exactly, without moving any files or backups.
        tables=self.store.query("SELECT name FROM sqlite_master WHERE type='table' AND name='workspace_bindings'")
        legacy=self.store.query('SELECT root FROM workspace_bindings WHERE session=?',(session,)) if tables else []
        root=legacy[0]['root'] if legacy else os.getenv('ARTORIAS_WORKSPACE')
        if root:
            w=self.register('本地工作空间',root,_legacy=bool(legacy))
        else:
            # Private managed default; not offered as a general authorization root.
            root=str(self.data/'workspaces'/'default')
            with self.store.tx() as c:
                old=c.execute('SELECT id FROM workspaces WHERE location_id=? AND root=?',(self.device_id,root)).fetchone()
                wid=old[0] if old else uid()
                c.execute('INSERT OR IGNORE INTO workspaces(id,name,location_id,root) VALUES(?,?,?,?)',(wid,'默认本地空间',self.device_id,root))
                if not old:c.execute('INSERT INTO workspace_grants(id,location_id,root,access,scope_run) VALUES(?,?,?,?,?)',(uid(),self.device_id,root,'write',''))
            w=self.get(wid)
        with self.store.tx() as c:
            c.execute('INSERT OR IGNORE INTO session_workspaces(session,workspace_id) VALUES(?,?)',(session,w['id']))
            c.execute('CREATE TABLE IF NOT EXISTS workspace_bindings(session TEXT PRIMARY KEY, root TEXT NOT NULL)')
            c.execute('INSERT OR IGNORE INTO workspace_bindings(session,root) VALUES(?,?)',(session,root))
        return self.get(self.store.query('SELECT workspace_id FROM session_workspaces WHERE session=?',(session,))[0]['workspace_id'])
    def select(self, session, wid):
        self.store.require(session);w=self.get(wid)
        if self.store.query("SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'"):
            for task in self.store.query('SELECT id FROM tasks WHERE session=?',(session,)):
                self.task_binding(task['id'],session)
        previous=self.store.query('SELECT workspace_id FROM session_workspaces WHERE session=?',(session,))
        with self.store.tx() as c:c.execute('INSERT INTO session_workspaces(session,workspace_id) VALUES(?,?) ON CONFLICT(session) DO UPDATE SET workspace_id=excluded.workspace_id',(session,wid))
        self.store.emit(session,None,'workspace.selected',{
            'previous_workspace_id':previous[0]['workspace_id'] if previous else None,
            'workspace_id':wid,'kind':w['kind']})
        return w
    def snapshot_run(self, run, session, task='', workspace_id=None):
        existing=self.store.query('SELECT workspace_id FROM run_workspace_bindings WHERE run=? AND task=?',(run,task))
        if existing:return self.get(existing[0]['workspace_id'])
        wid=workspace_id or self.current(session)['id']
        with self.store.tx() as c:c.execute('INSERT OR IGNORE INTO run_workspace_bindings(run,task,workspace_id) VALUES(?,?,?)',(run,task,wid))
        return self.get(self.store.query('SELECT workspace_id FROM run_workspace_bindings WHERE run=? AND task=?',(run,task))[0]['workspace_id'])
    def task_binding(self, task, session):
        rows=self.store.query('SELECT workspace_id FROM task_workspaces WHERE task=?',(task,))
        if rows:return self.get(rows[0]['workspace_id'])
        # Legacy Task: use its Session's imported v0.4 root before switching Session.
        wid=self.current(session)['id']
        with self.store.tx() as c:c.execute('INSERT OR IGNORE INTO task_workspaces(task,workspace_id) VALUES(?,?)',(task,wid))
        return self.get(wid)
    def grant(self, root, access='write', run=''):
        if access not in ('read','write'):raise ValueError('非法权限')
        raw=Path(root).expanduser()
        if not raw.is_absolute() or '..' in raw.parts:raise ValueError('授权必须使用绝对路径')
        root=str(raw.resolve());self._safe_root(root)
        with self.store.tx() as c:c.execute('INSERT INTO workspace_grants(id,location_id,root,access,scope_run) VALUES(?,?,?,?,?)',(uid(),self.device_id,root,access,run))
        return {'location_id':self.device_id,'root':root,'access':access,'scope_run':run}
    def authorize(self, location, target, action, run):
        grants=self.store.query("SELECT * FROM workspace_grants WHERE location_id=? AND (scope_run='' OR scope_run=?)",(location,run or ''))
        candidates=[]
        for grant in grants:
            if action=='write' and grant['access']!='write':continue
            base=Path(grant['root']);p=Path(target)
            if p==base or base in p.parents:candidates.append(grant)
        if not candidates:raise PermissionError('目标未授权；请使用 project_authorize 请求访问权限')
        return max(candidates,key=lambda g:len(Path(g['root']).parts))
