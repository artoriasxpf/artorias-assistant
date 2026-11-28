import asyncio
import io
import json
import shutil
import tarfile
from pathlib import Path
import pytest
from artorias_local_app.bootstrap import create_workspace
from artorias_server.storage.sqlite import Store
from artorias_server.tools.execution_service import ExecutionService
from artorias_device.tools.python_backend import unpack
from artorias_shared.execution.models import PythonRequest


class Backend:
    location='device'
    def __init__(self, action=None, status='succeeded'):
        self.action=action;self.calls=0;self.status=status
    def profile(self):return {'image':'sha256:test','location':'device'}
    async def run(self,plan,root):
        self.calls+=1
        if plan['request']['mode']=='workspace_copy':shutil.copytree(root/'seed',root/'work_result',dirs_exist_ok=True)
        if self.action:self.action(plan,root)
        return {'execution_status':self.status,'exit_code':0 if self.status=='succeeded' else 1,'stdout_tail':'done','stderr_tail':''}


def setup(tmp_path,backend):
    data=tmp_path/'data';data.mkdir()
    db=Store(data/'app.sqlite');sid=db.session();db.submit(sid,'execute','one');run=db.claim()
    ws=create_workspace(db,sid,run['id']);ws.write('script.py','print(42)',expected=None)
    service=ExecutionService(data,db,backend)
    return ws,service,db


def execute(service,ws,plan):return asyncio.run(service.execute(ws,plan))


def test_binary_output_and_idempotency_and_revert(tmp_path):
    raw=b'\x00\xff'*600_000
    backend=Backend(lambda p,r:(r/'outputs/report.bin').write_bytes(raw))
    ws,service,db=setup(tmp_path,backend)
    plan=service.prepare(ws,'call',{'path':'script.py','output':{'target':'reports'}})
    result=execute(service,ws,plan)
    assert result['execution_status']=='succeeded' and result['publish_status']=='succeeded'
    assert ws.read_bytes('reports/report.bin')==raw
    assert execute(service,ws,plan)==result and backend.calls==1
    ws.revert(result['published'][0]['change_id'])
    with pytest.raises(FileNotFoundError):ws.read_bytes('reports/report.bin')


def test_python_selects_and_pins_cloud_executor(tmp_path, monkeypatch):
    from artorias_server.workspace.catalog import WorkspaceCatalog
    from artorias_shared.workspace import ConnectorUnavailable

    monkeypatch.delenv('ARTORIAS_WORKSPACE',raising=False)
    device=Backend()
    _, service, db=setup(tmp_path,device)
    sid=db.query('SELECT id FROM sessions')[0]['id']
    catalog=WorkspaceCatalog(db)
    cloud=catalog.register('cloud','reports','cloud_storage')
    catalog.select(sid,cloud['id'])
    db.submit(sid,'execute in cloud','two')
    cloud_run=db.claim()
    workspace=create_workspace(db,sid,cloud_run['id'])
    workspace.write('script.py','print(42)',expected=None)

    with pytest.raises(ConnectorUnavailable,match='cloud Python'):
        service.prepare(workspace,'cloud-call',{'path':'script.py'})
    assert device.calls==0

    class CloudBackend(Backend):
        location='cloud'
        def profile(self):
            return {'location':'cloud','image':'sha256:cloud'}

    def write_cloud_output(plan, root):
        assert plan['execution_location']=='cloud'
        assert (root/'app/__entry__.py').read_text()=='print(42)'
        (root/'outputs/result.txt').write_text('cloud output')

    backend=CloudBackend(write_cloud_output)
    service=ExecutionService(tmp_path/'data',db,device,cloud_backend=backend)
    plan=service.prepare(workspace,'cloud-call',{'path':'script.py','output':{'target':'out'}})
    assert service.approval(plan)['execution_location']=='cloud'
    resumed=ExecutionService(tmp_path/'data',db,device,cloud_backend=backend)
    stored=resumed.prepare(workspace,'cloud-call',{'path':'script.py','output':{'target':'out'}})
    assert stored==plan
    assert execute(resumed,workspace,stored)['publish_status']=='succeeded'
    assert workspace.read('out/result.txt')=='cloud output'
    assert backend.calls==1 and device.calls==0


def test_input_mapping_and_approval_snapshot(tmp_path):
    ws,service,db=setup(tmp_path,Backend())
    ws.write('data/input.csv','a,b',expected=None)
    plan=service.prepare(ws,'call',{'path':'script.py','inputs':[{'resource':'data','mount_path':'source','directory':True}]})
    assert (service.root/plan['id']/'inputs/source/input.csv').read_text()=='a,b'
    assert not (service.root/plan['id']/'inputs/script.py').exists()
    assert service.approval(plan)['inputs']==['script.py','data/input.csv']
    ws.write('data/input.csv','changed')
    result=execute(service,ws,plan)
    assert result['execution_status']=='stale_plan' and service.backend.calls==0


def test_known_overwrite_conflict_and_partial(tmp_path):
    def action(plan,root):
        (root/'outputs/a').write_text('new');(root/'outputs/b').write_text('new')
        ws.write('out/a','external')
    ws,service,db=setup(tmp_path,Backend(action))
    ws.write('out/a','old',expected=None)
    plan=service.prepare(ws,'call',{'path':'script.py','output':{'target':'out','overwrite':'if_unchanged','expected_files':['a']}})
    result=execute(service,ws,plan)
    assert result['publish_status']=='partial'
    assert ws.read('out/a')=='external' and ws.read('out/b')=='new'
    assert len(result['conflicts'])==1
    assert (service.root/plan['id']/'artifacts'/result['artifacts'][0]['id']).exists()


def test_dynamic_existing_output_never_overwrites(tmp_path):
    ws,service,db=setup(tmp_path,Backend(lambda p,r:(r/'outputs/a').write_text('new')))
    ws.write('out/a','old',expected=None)
    plan=service.prepare(ws,'call',{'path':'script.py','output':{'target':'out','overwrite':'if_unchanged'}})
    assert execute(service,ws,plan)['publish_status']=='conflict'
    assert ws.read('out/a')=='old'


def test_successful_expected_overwrite(tmp_path):
    ws,service,db=setup(tmp_path,Backend(lambda p,r:(r/'outputs/a').write_text('new')))
    ws.write('out/a','old',expected=None)
    plan=service.prepare(ws,'call',{'path':'script.py','output':{'target':'out','overwrite':'if_unchanged','expected_files':['a']}})
    assert execute(service,ws,plan)['publish_status']=='succeeded'
    assert ws.read('out/a')=='new'


@pytest.mark.parametrize('allow_delete',[False,True])
def test_working_copy_diff_and_delete_authorization(tmp_path,allow_delete):
    def action(plan,root):
        (root/'work_result/a').write_text('changed');(root/'work_result/b').unlink()
        (root/'work_result/new').write_text('new')
    ws,service,db=setup(tmp_path,Backend(action))
    ws.write('project/a','old',expected=None);ws.write('project/b','remove',expected=None)
    ws.write('project/.env','secret',expected=None)
    plan=service.prepare(ws,'call',{'path':'script.py','mode':'workspace_copy','working_directory':'project','allow_delete':allow_delete})
    assert '.env' not in plan['baseline']
    result=execute(service,ws,plan)
    assert ws.read('project/a')=='changed' and ws.read('project/new')=='new'
    assert ws.read('project/.env')=='secret'
    assert (ws.head('project/b') is None)==allow_delete
    assert result['publish_status']==('succeeded' if allow_delete else 'partial')


@pytest.mark.parametrize('publish,status',[('stage_only','succeeded'),('auto_if_authorized','failed')])
def test_stage_only_and_nonzero_preserve_without_publishing(tmp_path,publish,status):
    ws,service,db=setup(tmp_path,Backend(lambda p,r:(r/'outputs/report').write_text('diagnostic'),status))
    plan=service.prepare(ws,'call',{'path':'script.py','output':{'target':'out','publish':publish}})
    result=execute(service,ws,plan)
    assert result['publish_status']=='not_requested' and len(result['artifacts'])==1
    assert ws.head('out/report') is None


def test_unknown_execution_is_not_replayed(tmp_path):
    ws,service,db=setup(tmp_path,Backend())
    plan=service.prepare(ws,'call',{'path':'script.py'})
    service.repo.transition(plan['id'],'awaiting_approval','running')
    assert execute(service,ws,plan)['execution_status']=='unknown' and service.backend.calls==0


def test_recover_publication_after_endpoint_commit(tmp_path):
    ws,service,db=setup(tmp_path,Backend(lambda p,r:(r/'outputs/a').write_text('ok')))
    plan=service.prepare(ws,'call',{'path':'script.py','output':{'target':'out','publish':'stage_only'}})
    result=execute(service,ws,plan)
    plan['request']['output']['publish']='auto_if_authorized'
    # Simulate a lost server publication receipt after an endpoint committed.
    from artorias_server.tools.execution_service import hashed
    item=result['artifacts'][0];op='publish_'+hashed([plan['id'],item['id'],item['target']])[:40]
    first=ws.write_bytes('out/a',b'ok',expected=None,operation_id=op)
    ws.write('out/a','later user change')
    result=service.publish(ws,plan,result)
    assert result['published'][0]['change_id']==first['change_id']
    assert ws.read('out/a')=='later user change'


def test_revoke_after_execution_blocks_publication(tmp_path):
    def action(plan,root):
        (root/'outputs/a').write_text('ok')
        with db.tx() as c:c.execute('DELETE FROM workspace_grants')
    ws,service,db=setup(tmp_path,Backend(action))
    plan=service.prepare(ws,'call',{'path':'script.py'})
    result=execute(service,ws,plan)
    assert not result.get('published')


@pytest.mark.parametrize('name,kind',[('../evil','file'),('/evil','file'),('link','symlink'),('fifo','fifo')])
def test_untrusted_tar_rejected(tmp_path,name,kind):
    buf=io.BytesIO()
    with tarfile.open(fileobj=buf,mode='w') as tf:
        item=tarfile.TarInfo(name)
        if kind=='symlink':item.type=tarfile.SYMTYPE;item.linkname='/etc/passwd'
        elif kind=='fifo':item.type=tarfile.FIFOTYPE
        else:item.size=1
        tf.addfile(item,io.BytesIO(b'x') if kind=='file' else None)
    with pytest.raises(ValueError):unpack(buf.getvalue(),tmp_path/'out')


def test_untrusted_output_symlink(tmp_path):
    ws,service,db=setup(tmp_path,Backend(lambda p,r:(r/'outputs/link').symlink_to('/etc/passwd')))
    plan=service.prepare(ws,'call',{'path':'script.py'})
    result=execute(service,ws,plan)
    assert result['execution_status']=='failed' and not result.get('published')


def test_invalid_modes_and_paths():
    with pytest.raises(ValueError):PythonRequest(path='a.py',mode='direct_mount')
    with pytest.raises(ValueError):PythonRequest(path='a.py',inputs=[{'resource':'a','mount_path':'../secret'}])
    with pytest.raises(ValueError):PythonRequest(path='a.py',timeout_seconds=301)


def test_new_id_for_new_tool_call_and_request_hash_guard(tmp_path):
    ws,service,db=setup(tmp_path,Backend())
    a=service.prepare(ws,'a',{'path':'script.py'})
    b=service.prepare(ws,'b',{'path':'script.py'})
    assert a['id']!=b['id']
    with pytest.raises(ValueError):service.prepare(ws,'a',{'path':'script.py','args':['other']})


def test_artifact_api_ownership(tmp_path):
    from fastapi.testclient import TestClient
    from artorias_server.api.app import create_http_app
    ws,service,db=setup(tmp_path,Backend(lambda p,r:(r/'outputs/report').write_bytes(b'\x00binary')))
    plan=service.prepare(ws,'call',{'path':'script.py','output':{'publish':'stage_only'}})
    result=execute(service,ws,plan);aid=result['artifacts'][0]['id']
    with TestClient(create_http_app(tmp_path/'data',db)) as client:
        headers={'x-artorias-token':client.get('/bootstrap').json()['token']}
        path=f'/api/sessions/{ws.session}/executions/{plan["id"]}/artifacts/{aid}'
        assert client.get(path).status_code==401
        response=client.get(path,headers=headers)
        assert response.status_code==200 and response.content==b'\x00binary'
        other=db.session()
        assert client.get(path.replace(ws.session,other),headers=headers).status_code==404


@pytest.mark.parametrize('entry', [{'path':'script.py'}, {'code':'print("内联")'}], ids=['path','code'])
def test_real_graph_approval_resume_publishes_once(tmp_path, entry):
    from functools import partial
    from langchain_core.messages import AIMessage
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from artorias_server.runtime.service import DeepAgentRuntime
    from artorias_server.tools.registry import build_tools
    class Model(GenericFakeChatModel):
        def bind_tools(self,*args,**kwargs):return self
    def write_output(plan, root):
        assert (root/'app/__entry__.py').read_text() == entry.get('code', 'print(42)')
        (root/'outputs/a.bin').write_bytes(b'\x00\xff')

    backend=Backend(write_output)
    ws,service,db=setup(tmp_path,backend)
    run=db.query('SELECT * FROM runs')[0]
    model=Model(disable_streaming=True,messages=iter([
        AIMessage(content='',tool_calls=[{'name':'run_python','args':{**entry,'args':['hello world'],'output':{'target':'out'}},'id':'actual-call','type':'tool_call'}]),
        AIMessage(content='saved')]))
    engine=DeepAgentRuntime(tmp_path/'data',db,workspace_factory=lambda r:ws,model_factory=lambda:model,tool_factory=partial(build_tools,execution_service=service))
    async def collect(r):return [x async for x in engine.execute(r)]
    first=asyncio.run(collect(run))
    assert first[-1][0]=='approval.requested' and backend.calls==0
    approval = first[-1][1]['requests'][0]
    assert approval['output']['target']=='out'
    if 'code' in entry:
        import hashlib
        assert approval['path'] == '内联 Python 代码'
        assert approval['script_sha256'] == hashlib.sha256(entry['code'].encode()).hexdigest()
        assert approval['inputs'] == []
    db.finish(run,'waiting',approval=first[-1][1]);db.decide(run['id'],'approve')
    second=asyncio.run(collect(db.claim()))
    assert second[-1][0]=='assistant.final' and backend.calls==1
    assert ws.read_bytes('out/a.bin')==b'\x00\xff'


def test_inline_code_revalidates_staging_and_rejects_changed_request(tmp_path):
    backend = Backend()
    ws, service, _ = setup(tmp_path, backend)
    request = {'code': 'print(42)', 'output': {'publish': 'stage_only'}}
    plan = service.prepare(ws, 'call', request)
    assert service.prepare(ws, 'call', request) == plan
    with pytest.raises(ValueError, match='同一工具调用参数发生变化'):
        service.prepare(ws, 'call', {**request, 'code': 'print(43)'})
    entry = service.root / plan['id'] / 'app/__entry__.py'
    entry.write_text('print(43)')
    assert execute(service, ws, plan)['execution_status'] == 'stale_plan'
    assert backend.calls == 0


@pytest.mark.parametrize('entry', [{}, {'path': 'script.py', 'code': 'print(42)'}, {'code': ' '},
                                  {'code': '字' * 21846}])
def test_inline_code_requires_one_valid_entrypoint(entry):
    with pytest.raises(ValueError):
        PythonRequest.model_validate(entry)


def test_cancel_records_state_and_no_publication(tmp_path):
    class CancelBackend(Backend):
        async def run(self,*args):raise asyncio.CancelledError()
    ws,service,db=setup(tmp_path,CancelBackend())
    plan=service.prepare(ws,'call',{'path':'script.py'})
    with pytest.raises(asyncio.CancelledError):execute(service,ws,plan)
    assert service.repo.get(plan['id'])['status']=='cancelled'
    assert not (service.root/plan['id']/'inputs').exists()


def test_local_docker_integration(tmp_path):
    """Opt-in test requires a running Docker daemon and a preloaded Python image."""
    import os
    if os.getenv('ARTORIAS_TEST_DOCKER')!='1':pytest.skip('set ARTORIAS_TEST_DOCKER=1 for real Docker integration')
    from artorias_device.tools.python_backend import DockerPythonBackend
    ws,service,db=setup(tmp_path,DockerPythonBackend())
    ws.write('script.py',"from pathlib import Path\nPath('/outputs/report.bin').write_bytes(b'\\x00ok')\nprint('done')")
    plan=service.prepare(ws,'docker',{'path':'script.py','output':{'target':'out'}})
    result=execute(service,ws,plan)
    assert result['execution_status']=='succeeded',result
    assert result['publish_status']=='succeeded',result
    assert ws.read_bytes('out/report.bin')==b'\x00ok'


def test_docker_command_timeout_includes_process_exit(monkeypatch):
    from artorias_device.tools.python_backend import DockerPythonBackend
    import sys
    original=asyncio.create_subprocess_exec
    async def spawn(*args,**kwargs):
        return await original(sys.executable,'-c','import os,time;os.close(1);os.close(2);time.sleep(60)',**kwargs)
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    with pytest.raises(TimeoutError):asyncio.run(DockerPythonBackend().command(['test'],timeout=.1))


def test_docker_isolation_command_and_safe_collection(tmp_path,monkeypatch):
    from artorias_device.tools.python_backend import DockerPythonBackend
    import sys
    captured=[]
    def archive():
        buf=io.BytesIO()
        with tarfile.open(fileobj=buf,mode='w') as tf:
            item=tarfile.TarInfo('outputs/report.bin');item.size=3;tf.addfile(item,io.BytesIO(b'abc'))
        return buf.getvalue()
    class MockDocker(DockerPythonBackend):
        async def command(self,args,**kwargs):
            captured.append(args)
            if args[0]=='inspect':return b'{"Running":true}'
            if args[0]=='exec':return archive()
            return b''
    original=asyncio.create_subprocess_exec
    async def spawn(*args,**kwargs):
        captured.append(list(args))
        return await original(sys.executable,'-c','print("fixture log")',**kwargs)
    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    for name in ('app','inputs','seed','outputs'):(tmp_path/name).mkdir()
    plan={'id':'exec_fixture','profile':{'image':'sha256:immutable'},'request':{'mode':'isolated','args':['a b','$(no-shell)'],'timeout_seconds':1}}
    result=asyncio.run(MockDocker().run(plan,tmp_path))
    assert result['execution_status']=='succeeded'
    assert (tmp_path/'outputs/report.bin').read_bytes()==b'abc'
    create=captured[0]
    assert '--network=none' in create and '--read-only' in create and '--cap-drop=ALL' in create
    assert '--user=65534:65534' in create and 'sha256:immutable' in create
    assert all(create[i+1].endswith(',readonly') for i,x in enumerate(create) if x=='--mount')
    command=next(x for x in captured if x[:2]==['docker','exec'])
    assert command[-2:]==['a b','$(no-shell)']
    assert captured[-1]==['rm','-f','artorias-exec_fixture']


def test_startup_marks_uncertain_execution_and_stops_container(tmp_path):
    class StopBackend(Backend):
        async def stop(self,execution_id):self.stopped=execution_id
    backend=StopBackend();ws,service,db=setup(tmp_path,backend)
    plan=service.prepare(ws,'call',{'path':'script.py'})
    service.repo.transition(plan['id'],'awaiting_approval','running')
    asyncio.run(service.recover(lambda ctx:ws))
    row=service.repo.get(plan['id'])
    assert row['status']=='unknown' and row['result']['container_stopped']
    assert backend.stopped==plan['id'] and backend.calls==0


def test_startup_resumes_collected_publication_without_execution(tmp_path):
    ws,service,db=setup(tmp_path,Backend(lambda p,r:(r/'outputs/report').write_text('ready')))
    plan=service.prepare(ws,'call',{'path':'script.py','output':{'target':'out'}})
    # Persist collected evidence, then emulate a restart before publication.
    original=service.publish
    service.publish=lambda ws,plan,result:result
    result=execute(service,ws,plan)
    assert service.repo.get(plan['id'])['status']=='collected'
    service.publish=original
    asyncio.run(service.recover(lambda ctx:ws))
    assert ws.read('out/report')=='ready' and service.backend.calls==1


def test_permission_revoked_before_execution(tmp_path):
    ws,service,db=setup(tmp_path,Backend())
    plan=service.prepare(ws,'call',{'path':'script.py'})
    with db.tx() as c:c.execute('DELETE FROM workspace_grants')
    result=execute(service,ws,plan)
    assert result['execution_status']=='stale_plan' and service.backend.calls==0
    assert not (service.root/plan['id']/'inputs').exists()


def test_remembered_python_permission_matches_only_same_plan(tmp_path):
    from artorias_server.permissions import fingerprint
    from artorias_server.tools.executor import WorkspaceToolExecutor
    from artorias_shared.execution.models import PythonRequest
    from artorias_server.workspace.routing import WorkspaceService

    backend = Backend()
    ws, service, store = setup(tmp_path, backend)
    request = PythonRequest(path='script.py')
    plan = service.prepare(ws, 'first', request)
    resource = service.permission_rule(ws, plan)
    permission_id = ws.permissions.request(ws, 'first', dict(
        location_id=resource['location_id'], workspace_id=resource['workspace_id'],
        action='execute', resource_kind='command', rule=resource, rule_hash=fingerprint(resource),
    ), plan['plan_hash'])
    run = store.query('SELECT * FROM runs WHERE id=?', (ws.run,))[0]
    store.finish(run, 'waiting', approval={'requests': [{
        'request_id': 'interrupt', 'permission_request_id': permission_id, 'allow_remember': True,
    }]})
    store.decide(ws.run, 'remember')
    store.claim()
    store.finish(run, 'completed')
    store.submit(ws.session, 'second execution', 'second')
    next_run = store.claim()
    next_ws = WorkspaceService(store, ws.session, next_run['id'], registry=ws.registry)
    tool = WorkspaceToolExecutor(next_ws, execution_service=service)

    def requires_no_approval(_):
        pytest.fail('unchanged script should reuse its exact Session rule')

    result = asyncio.run(tool.run_python('second', request, requires_no_approval, lambda *_: None))
    assert result['execution_status'] == 'succeeded'
    requests = []
    result = asyncio.run(tool.run_python(
        'different-args', PythonRequest(path='script.py', args=['new']),
        lambda details: requests.append(details) or 'reject', lambda *_: None,
    ))
    assert result['execution_status'] == 'rejected'
    assert requests[0]['rule']['request']['args'] == ['new']

    _, version = next_ws.read_version('script.py')
    next_ws.write('script.py', 'print(43)', expected=version)
    result = asyncio.run(tool.run_python(
        'changed-script', request,
        lambda details: requests.append(details) or 'reject', lambda *_: None,
    ))
    assert result['execution_status'] == 'rejected'
    assert requests[1]['script_sha256'] != plan['inputs'][0]['sha256']
    assert backend.calls == 1
