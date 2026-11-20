"""Local Python orchestration: immutable approval plan, artifacts, conditional publish."""
import asyncio
import fnmatch
import hashlib
import json
import os
import shutil
import stat
from pathlib import Path
from artorias_shared.execution.models import PythonRequest, safe_relative
from artorias_shared.ports.execution import PythonExecutionBackend
from artorias_shared.workspace import ConnectorUnavailable
from artorias_server.storage.executions import ExecutionRepository
from .executor import ExecutorRegistry

MAX_FILE=32_000_000
MAX_TOTAL=96_000_000
MAX_FILES=500


def hashed(value):
    return hashlib.sha256(value if isinstance(value,bytes) else json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def join(base, name):
    return base.rstrip('/')+'/'+safe_relative(name) if base not in ('','.') else safe_relative(name)


def excluded(name):
    return any(p in ('.git','.venv','__pycache__','.ssh','.aws') or p=='.env' or p.startswith('.env.') for p in Path(name).parts)


def scan(root):
    """Never follow executable-created links or read unbounded files."""
    result={};total=0
    for directory,dirs,files in os.walk(root,followlinks=False):
        for name in dirs+files:
            path=Path(directory)/name;info=path.lstat()
            if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise ValueError('产物包含链接或特殊文件')
            if stat.S_ISDIR(info.st_mode):continue
            if info.st_nlink!=1 or info.st_size>MAX_FILE:raise ValueError('产物链接或大小非法')
            total+=info.st_size
            if total>MAX_TOTAL or len(result)>=MAX_FILES:raise ValueError('产物超出配额')
            data=path.read_bytes()
            if len(data)!=info.st_size:raise ValueError('产物在收集时发生变化')
            relative=safe_relative(path.relative_to(root).as_posix())
            result[relative]={'sha256':hashed(data),'size':len(data)}
    return result


class ExecutionService:
    def __init__(self, data, store, backend: PythonExecutionBackend | None = None, *, cloud_backend: PythonExecutionBackend | None = None):
        self.root=Path(data).resolve()/'executions';self.root.mkdir(parents=True,exist_ok=True)
        self.repo=ExecutionRepository(store);self.backend=backend
        self.backends=ExecutorRegistry()
        if backend is not None:
            self.backends.register(backend.location,backend)
        if cloud_backend is not None:
            if cloud_backend.location != 'cloud':
                raise ValueError('云端 Python 适配器的 location 必须是 cloud')
            self.backends.register('cloud',cloud_backend)

    def _plan_location(self, plan):
        # Older approval plans record location only in the execution profile.
        hint=plan['request'].get('execution_hint','auto')
        return plan.get('execution_location') or plan['profile'].get('location') or ('device' if hint=='auto' else hint)

    def _backend(self, location):
        try:
            return self.backends.get(location)
        except ConnectorUnavailable:
            raise ConnectorUnavailable(f'未配置 {location} Python 执行器；不会切换执行位置') from None

    async def recover(self, workspace_factory):
        """Single-process startup reconciliation; never restart uncertain Python code."""
        rows=self.repo.store.query("SELECT id,status FROM python_executions WHERE status IN ('running','collected')")
        for item in rows:
            row=self.repo.get(item['id']);plan=row['plan']
            if item['status']=='running':
                stopped=False
                try:
                    backend=self._backend(self._plan_location(plan))
                    if hasattr(backend,'stop'):
                        await backend.stop(plan['id']);stopped=True
                except Exception:pass
                self.repo.save(plan['id'],'unknown',{'execution_id':plan['id'],'execution_status':'unknown',
                    'publish_status':'not_requested','container_stopped':stopped,
                    'error':'上次进程中断，脚本结果无法核实；不会自动重跑。'})
            else:
                context=plan['context']
                try:
                    ws=workspace_factory(context)
                    self.publish(ws,plan,row['result'])
                except Exception:
                    # Keep collected evidence for a future retry; never replay the script.
                    continue

    def prepare(self, ws, call_id, request):
        req=PythonRequest.model_validate(request).model_dump()
        context={'session':ws.session,'run':ws.run,'task':ws.task,'step':getattr(ws,'step',None),'call':call_id}
        eid='exec_'+hashed(context)[:40];rh=hashed(req)
        existing=self.repo.get(eid)
        if existing:
            if existing['request_hash']!=rh:raise ValueError('同一工具调用参数发生变化')
            return existing['plan']
        location=req['execution_hint']
        if location=='auto':
            location={'device_fs':'device','cloud_storage':'cloud'}[ws.describe()['kind']]
        backend=self._backend(location)
        if req['mode']=='workspace_copy' and not req['working_directory']:raise ValueError('workspace_copy 需要 working_directory')
        if req['allow_delete'] and req['mode']!='workspace_copy':raise ValueError('删除仅支持工作副本模式')
        root=self.root/eid
        if root.exists():shutil.rmtree(root)  # orphan preparation, never an installed plan
        for name in ('app','inputs','seed','artifacts','outputs','work_result'):(root/name).mkdir(parents=True,exist_ok=True)
        entries=[];total=0;destinations=set()
        def stage(resource, destination, baseline=None):
            nonlocal total
            if len(entries)>=MAX_FILES:raise ValueError('输入文件过多')
            if destination in destinations:raise ValueError('输入路径映射重复')
            destinations.add(destination)
            data,version=ws.read_binary_version(resource)
            total+=len(data)
            if len(data)>MAX_FILE or total>MAX_TOTAL:raise ValueError('输入超过配额')
            dest=root/destination;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(data)
            entries.append({'resource':resource,'version':version,'sha256':hashed(data),'staged':destination})
            if baseline is not None:baseline[destination.removeprefix('seed/')]=version
        try:
            stage(req['path'],'app/__entry__.py')
            if not req['path'].endswith('.py'):raise ValueError('入口必须为 Python 脚本')
            if req['code_directory']:
                for name in ws.files_under(req['code_directory']):
                    if not excluded(name):stage(join(req['code_directory'],name),'app/'+safe_relative(name))
            for item in req['inputs']:
                names=ws.files_under(item['resource']) if item['directory'] else ['']
                for name in names:
                    if name and excluded(name):continue
                    stage(join(item['resource'],name) if name else item['resource'], 'inputs/'+safe_relative(join(item['mount_path'],name) if name else item['mount_path']))
            baseline={}
            if req['mode']=='workspace_copy':
                for name in ws.files_under(req['working_directory']):
                    if excluded(name):continue
                    target=join(req['working_directory'],name)
                    ws.check_access(target,'write')
                    stage(target,'seed/'+safe_relative(name),baseline)
                ws.check_access(join(req['working_directory'],'__permission_probe__'),'write')
            target=req['output']['target'] or 'artifacts/'+eid
            if req['output']['publish']=='auto_if_authorized':ws.check_access(join(target,'__permission_probe__'),'write')
            targets={}
            for name in req['output']['expected_files']:
                ref=join(target,name);ws.check_access(ref,'write')
                targets[name]=ws.head(ref) if req['output']['overwrite']=='if_unchanged' else None
            plan={'id':eid,'request_hash':rh,'context':context,'request':req,'inputs':entries,
                  'baseline':baseline,'target':target,'targets':targets,'profile':backend.profile(),
                  'execution_location':location,
                  'network':'none','input_bytes':total}
            plan['plan_hash']=hashed(plan)
            self.repo.install(plan)
            return plan
        except BaseException:
            shutil.rmtree(root,ignore_errors=True);raise

    def approval(self,plan):
        req=plan['request']
        return {'kind':'code_execution','execution_id':plan['id'],'path':req['path'],'plan_hash':plan['plan_hash'],
                'mode':req['mode'],'inputs':[x['resource'] for x in plan['inputs']],
                'script_sha256':plan['inputs'][0]['sha256'],'execution_location':self._plan_location(plan),
                'environment':plan['profile'],'network':'none','timeout_seconds':req['timeout_seconds'],
                'output':dict(req['output'],target=plan['target']),'working_directory':req['working_directory'],
                'allow_delete':req['allow_delete'],'notice':'仅提交已批准范围内的产物；覆盖需匹配执行前版本。'}

    def reject(self,plan):
        result={'execution_id':plan['id'],'execution_status':'rejected','publish_status':'not_requested','rejected':True}
        self.repo.save(plan['id'],'rejected',result)
        shutil.rmtree(self.root/plan['id'],ignore_errors=True)
        return result

    async def execute(self,ws,plan):
        eid=plan['id'];root=self.root/eid;row=self.repo.get(eid)
        if row['status'] in ('completed','failed','cancelled','rejected'):return row['result']
        if row['status']=='collected':return self.publish(ws,plan,row['result'])
        if row['status']!='awaiting_approval':
            return {'execution_id':eid,'execution_status':'unknown','publish_status':'not_requested','error':'执行状态不确定，禁止自动重跑；请检查设备容器与日志'}
        backend=self._backend(self._plan_location(plan))
        # Bind approval to the staged bytes and current resource permissions/versions.
        try:
            for entry in plan['inputs']:
                data,version=ws.read_binary_version(entry['resource'])
                if version!=entry['version'] or hashed(data)!=entry['sha256'] or hashed((root/entry['staged']).read_bytes())!=entry['sha256']:
                    raise ValueError('审批后的输入发生变化，请重新发起调用')
        except (ValueError,PermissionError,OSError) as exc:
            result={'execution_id':eid,'execution_status':'stale_plan','publish_status':'not_requested','error':str(exc)}
            self.repo.save(eid,'failed',result)
            for name in ('inputs','app','seed'):shutil.rmtree(root/name,ignore_errors=True)
            return result
        if not self.repo.transition(eid,'awaiting_approval','running'):raise ValueError('执行已被其他调用领取')
        # Execution lifecycle detail travels inside the run_python tool result; no
        # separate execution.* events are written to the session event table.
        try:
            result=await backend.run(plan,root)
            result=dict(result,execution_id=eid,publish_status='not_requested',artifacts=[],published=[],conflicts=[])
            outputs=scan(root/'outputs')
            work=scan(root/'work_result') if plan['request']['mode']=='workspace_copy' else {}
            items=[]
            for name,meta in outputs.items():
                if any(fnmatch.fnmatchcase(name,p) for p in plan['request']['output']['include']):
                    items.append(dict(meta,path=name,source='outputs',target=join(plan['target'],name),expected=plan['targets'].get(name),action='write'))
            if plan['request']['mode']=='workspace_copy':
                for name,meta in work.items():
                    if meta['sha256']!=plan['baseline'].get(name):
                        if excluded(name):raise ValueError('工作副本产生受排除路径')
                        items.append(dict(meta,path=name,source='work_result',target=join(plan['request']['working_directory'],name),expected=plan['baseline'].get(name),action='write'))
                for name,version in plan['baseline'].items():
                    if name not in work:
                        items.append({'path':name,'source':'work_result','target':join(plan['request']['working_directory'],name),'expected':version,'action':'delete','sha256':None,'size':0})
            if len(items)>MAX_FILES or sum(item['size'] for item in items)>MAX_TOTAL:raise ValueError('产物总量超过配额')
            canonical=set()
            for index,item in enumerate(items):
                try:key=ws.path(item['target'])
                except PermissionError:key=item['target']
                if key in canonical:raise ValueError('多个产物写入同一目标')
                canonical.add(key)
                item['id']=f'{eid}_{index}'
                if item['action']=='write':
                    artifact=root/'artifacts'/item['id'];shutil.copyfile(root/item['source']/item['path'],artifact)
                result['artifacts'].append(item)
            self.repo.save(eid,'collected',result)
            return self.publish(ws,plan,result)
        except asyncio.CancelledError:
            result={'execution_id':eid,'execution_status':'cancelled','publish_status':'not_requested'}
            self.repo.save(eid,'cancelled',result);raise
        except Exception as exc:
            result={'execution_id':eid,'execution_status':'failed','publish_status':'not_requested','error':str(exc)[:2000]}
            self.repo.save(eid,'failed',result);return result
        finally:
            for name in ('inputs','app','seed','outputs','work_result','collection'):shutil.rmtree(root/name,ignore_errors=True)

    def publish(self,ws,plan,result):
        # Publishing is separately recoverable; each endpoint write uses an operation ID.
        if result['execution_status']!='succeeded' or plan['request']['output']['publish']=='stage_only':
            self.repo.save(plan['id'],'completed',result);return result
        result['published']=[];result['conflicts']=[]
        for item in result['artifacts']:
            op='publish_'+hashed([plan['id'],item['id'],item['target']])[:40]
            previous=self.repo.publication(op)
            if previous:
                result['published'].append(json.loads(previous['result']));continue
            try:
                if item['action']=='delete':
                    if not plan['request']['allow_delete']:raise ValueError('删除未获授权，原文件保留')
                    change=ws.delete_version(item['target'],expected=item['expected'],operation_id=op)
                else:
                    data=(self.root/plan['id']/'artifacts'/item['id']).read_bytes()
                    if hashed(data)!=item['sha256']:raise ValueError('产物校验失败')
                    change=ws.write_bytes(item['target'],data,expected=item['expected'],operation_id=op)
                saved={'artifact_id':item['id'],'target':item['target'],'change_id':change['change_id']}
                self.repo.published(plan['id'],op,item['target'],saved);result['published'].append(saved)
            except Exception as exc:
                result['conflicts'].append({'target':item['target'],'reason':str(exc)})
        result['publish_status']='partial' if result['conflicts'] and result['published'] else 'conflict' if result['conflicts'] else 'succeeded'
        self.repo.save(plan['id'],'completed',result)
        return result
