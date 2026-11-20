"""Resolve and authorize each operation; never reinterpret a rollback via current Session."""
from pathlib import Path
from .catalog import WorkspaceCatalog
from .connectors import ConnectorRegistry
from artorias_shared.workspace import ConnectorUnavailable, WorkspaceContext, UNSET_VERSION
_UNSET = UNSET_VERSION

class WorkspaceService:
    def __init__(self, store, session, run=None, task=None, registry=None):
        self.store,self.session,self.run,self.task=store,session,run,task
        self.catalog=WorkspaceCatalog(store)
        self.registry=registry or ConnectorRegistry()
        w=self.catalog.task_binding(task,session) if task else self.catalog.current(session)
        self.workspace=self.catalog.snapshot_run(run,session,task or '',w['id']) if run else w
        self.root=self.workspace['root']
    def describe(self):
        return {k:self.workspace[k] for k in ('id','name','kind','location_id','root')}
    def _resolve(self, path, action, *, directory=False, create_default=False):
        if not isinstance(path,str) or not path or '\x00' in path:raise ValueError('路径不能为空')
        w=self.workspace
        if path.startswith('workspace://'):
            wid,sep,path=path[len('workspace://'):].partition('/')
            if not sep:raise ValueError('必须指定空间内的文件路径')
            if not path and not directory:raise ValueError('必须指定空间内的文件路径')
            w=self.catalog.get(wid)
            if Path(path).is_absolute():raise ValueError('空间引用只能使用相对路径')
        elif Path(path).is_absolute():
            # Absolute host paths explicitly refer to this installation's registered device.
            w=dict(w,kind='device_fs',location_id=self.catalog.device_id,
                   blob_root=str(self.catalog.data/'blobs'),id=None)
        p=Path(path)
        if '..' in p.parts or '\\' in path:raise ValueError('非法路径')
        target=p if p.is_absolute() else Path(w['root'])/p
        grant=self.catalog.authorize(w['location_id'],str(target),action,self.run)
        # Use the grant root as boundary; workspace is only the default resolution root.
        routed=dict(w,root=grant['root'])
        if directory:routed['create_root']=create_default
        connector=self.registry.get(w['kind'],w['location_id'])
        ws=connector.open(routed,WorkspaceContext(self.session,self.run,self.task))
        relative=target.relative_to(Path(grant['root'])).as_posix()
        if not directory:
            if relative=='.':raise ValueError('需要文件路径而不是授权目录')
            ws.path(relative)  # Endpoint enforces canonical path/symlink checks too.
        return ws,relative,w,target
    def path(self,path):
        _,_,w,target=self._resolve(path,'read')
        return f"{w['kind']}:{w['location_id']}:{target}"
    def read_version(self,path):
        ws,p,_,_=self._resolve(path,'read');return ws.read_version(p)
    def read(self,path):return self.read_version(path)[0]
    def read_bytes(self,path):
        ws,p,_,_=self._resolve(path,'read');return ws.read_bytes(p)
    def head(self,path):
        ws,p,_,_=self._resolve(path,'read');return ws.head(p)
    def write(self,path,content,expected=_UNSET,reverts=None):
        ws,p,w,target=self._resolve(path,'write')
        result=ws.write(p,content,expected,reverts)
        return dict(result,resource={'backend':w['kind'],'location_id':w['location_id'],'path':str(target)})
    def read_binary_version(self, path):
        ws,p,_,_=self._resolve(path,'read');return ws.read_binary_version(p)

    def check_access(self, path, action='read'):
        self._resolve(path, action)

    def files_under(self, directory):
        ws,relative,_,_=self._resolve(directory,'read',directory=True)
        if hasattr(ws,'files_under'):
            return ws.files_under(relative)
        # Older injected connectors only support listing their root.
        if relative=='.':return ws.files()
        prefix=ws.path(relative).rstrip('/')+'/'
        return [name[len(prefix):] for name in ws.files() if name.startswith(prefix)]

    def list_directory(self, directory):
        endpoint, relative, _, _ = self._resolve(directory, 'read', directory=True, create_default=directory=='.')
        list_directory = getattr(endpoint, 'list_directory', None)
        if list_directory is None:
            raise ConnectorUnavailable('目标连接器不支持目录列表')
        return list_directory(relative)

    def tree(self, directory, *, max_depth, max_entries, include_hidden):
        endpoint, relative, _, _ = self._resolve(directory, 'read', directory=True, create_default=directory=='.')
        tree = getattr(endpoint, 'tree', None)
        if tree is None:
            raise ConnectorUnavailable('目标连接器不支持目录树')
        return tree(relative, max_depth=max_depth, max_entries=max_entries, include_hidden=include_hidden)

    def write_bytes(self, path, content, *, expected, operation_id=None):
        ws,p,w,target=self._resolve(path,'write')
        result=ws.write_bytes(p,content,expected=expected,operation_id=operation_id)
        return dict(result, resource={'backend':w['kind'],'location_id':w['location_id'],'path':str(target)})

    def delete_version(self, path, *, expected, operation_id=None):
        ws,p,_,_=self._resolve(path,'write')
        return ws.delete_version(p,expected=expected,operation_id=operation_id)

    def delete(self,path):
        ws,p,_,_=self._resolve(path,'write');return ws.delete(p)
    def files(self):
        w=self.workspace
        self.catalog.authorize(w['location_id'],w['root'],'read',self.run)
        return self.registry.get(w['kind'],w['location_id']).open(w,WorkspaceContext(self.session,self.run,self.task)).files()
    def revert(self,cid):
        record=self.registry.find_change(cid,self.session)
        if not record:raise ValueError('变更不存在')
        backend=record['backend'] or 'device_fs';location=record['location_id'] or self.catalog.device_id
        target=Path(record['root'])/record['path']
        if backend=='cloud_storage':
            loc=self.store.query('SELECT * FROM workspace_locations WHERE id=?',(location,))[0]
            logical=target.relative_to(Path(loc['root'])).as_posix()
            self.catalog.authorize(location,logical,'write',self.run)
        else:self.catalog.authorize(location,str(target),'write',self.run)
        # Original root and blob location are authoritative, including pre-v0.5 records.
        w=dict(id=record['workspace_id'],kind=backend,location_id=location,root=record['root'],blob_root=record['blob_root'] or str(self.catalog.data/'blobs'))
        connector=self.registry.get(backend,location)
        if backend=='cloud_storage':
            w['location_root']=loc['root'];w['root']=str(Path(record['root']).relative_to(Path(loc['root'])))
        ws=connector.open(w,WorkspaceContext(self.session,self.run,record['task_id']))
        return ws.revert(cid)
    def grant_access(self, path, access):
        """Persist a grant after the caller has obtained approval.

        Trusted application entry point, never expose directly as an unapproved tool.
        Endpoint authorization remains enforced on subsequent operations.
        """
        if access not in ('read', 'write'):
            raise ValueError('access 必须为 read 或 write')
        return self.catalog.grant(path, access, self.run)
