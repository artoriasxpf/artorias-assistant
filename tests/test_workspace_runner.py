import asyncio
from pathlib import Path
import sys
import pytest
from fastapi.testclient import TestClient

from artorias_device.tools.workspace_runner import WorkspaceRunner
from artorias_local_app.runner import create_runner_app
from artorias_shared.execution.workspace import WorkspaceJob


def job(request, suffix='a'):
    return WorkspaceJob.model_validate(dict(execution_id='exec_'+suffix*40,workspace_id='project',
                    location_id='mac',kind='device_fs',request=request))


def test_runner_transport_approval_and_replay(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    runner = WorkspaceRunner(tmp_path/'state',lambda _:project)
    calls = []
    async def launch(work, plan, tmp):
        calls.append(work.execution_id)
        return dict(execution_status='succeeded',stdout_tail='ok')
    monkeypatch.setattr(runner,'launch',launch)
    token = 'a'*32
    payload = job({'argv':['git','status']}).model_dump(exclude_unset=True)
    with TestClient(create_runner_app(runner,token)) as client:
        assert client.post('/prepare',json=payload).status_code == 401
        client.headers.update({'Authorization':'Bearer '+token})
        if sys.platform.startswith('linux'):
            runner.sandbox = 'bubblewrap'
            monkeypatch.setattr('artorias_device.tools.workspace_runner.shutil.which',lambda x:'/usr/bin/'+x)
        plan = client.post('/prepare',json=payload).json()
        ref = {'execution_id':payload['execution_id']}
        assert client.post('/start',json=dict(ref,plan_hash='0'*64)).status_code == 409
        for _ in range(2):
            client.post('/start',json=dict(ref,plan_hash=plan['plan_hash'])).raise_for_status()
        assert client.post('/status',json=ref).json()['execution_status']=='succeeded'
        assert calls==[payload['execution_id']]


@pytest.mark.skipif(sys.platform!='darwin',reason='real macOS Seatbelt')
def test_native_workspace_modes(tmp_path):
    project = tmp_path/'project'
    project.mkdir()
    (project/'data.txt').write_text('before')
    runner = WorkspaceRunner(tmp_path/'runner',lambda _:project)
    async def invoke(request, suffix):
        work = job(request,suffix)
        plan = await runner.prepare(work)
        await runner.start(work.execution_id,plan['plan_hash'])
        await runner.active[work.execution_id]
        return await runner.status(work.execution_id)
    async def scenario():
        source = "from pathlib import Path\np=Path('data.txt')\nprint(p.read_text())\np.write_text('after')"
        ro = await invoke({'code':source,'mode':'workspace_ro'},'a')
        assert ro['execution_status']=='failed' and 'PermissionError' in ro.get('stderr_tail',''),ro
        assert (project/'data.txt').read_text()=='before'
        rw = await invoke({'code':source,'mode':'workspace_rw'},'b')
        assert rw['execution_status']=='succeeded',rw
        assert (project/'data.txt').read_text()=='after'
        search = await invoke({'argv':['rg','-n','after','.']},'c')
        assert 'data.txt:1:after' in search.get('stdout_tail',''),search
        import subprocess
        subprocess.run(['git','init','-q',str(project)],check=True)
        status = await invoke({'argv':['git','status','--short']},'d')
        assert '?? data.txt' in status.get('stdout_tail',''),status
    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform!='darwin',reason='real Seatbelt + uv')
def test_remote_workspace_tool_routes_and_publishes(tmp_path,monkeypatch):
    import httpx
    from artorias_local_app.bootstrap import create_workspace
    from artorias_local_app.runner_staging import RunnerStaging
    from artorias_server.storage.sqlite import Store
    from artorias_server.workspace.catalog import WorkspaceCatalog
    from artorias_server.tools.execution_service import ExecutionService
    from artorias_server.tools.registry import build_tools
    from artorias_server.tools.workspace_execution import RunnerClient

    db = Store(tmp_path/'runtime.sqlite')
    catalog = WorkspaceCatalog(db)
    sid = db.session()
    project = tmp_path/'mounted-on-runner'
    project.mkdir()
    (project/'source.txt').write_text('REMOTE')
    ws_record = catalog.register('cloud','logical-only','cloud_storage')
    catalog.select(sid,ws_record['id'])
    ws = create_workspace(db,sid)
    state = tmp_path/'runner'
    runner = WorkspaceRunner(state,lambda _:project,staging=RunnerStaging(state/'staging'))
    token = 't'*32
    app = create_runner_app(runner,token)
    client = RunnerClient('http://127.0.0.1:8766',token)
    async def rpc(method,body):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as http:
            response = await http.post('/'+method,json=body,headers=client.headers)
            response.raise_for_status()
            return response.json()
    monkeypatch.setattr(client,'rpc',rpc)
    service = ExecutionService(tmp_path/'runtime',db,runners={('cloud_storage','cloud-default'):client})
    approvals = []
    def approve(card):
        approvals.append(card)
        return 'approve'
    monkeypatch.setattr('artorias_server.tools.registry.interrupt',approve)
    tools = {t.name:t for t in build_tools(ws,lambda *_:None,execution_service=service)}
    async def invoke(tool,args,call):
        message = await tools[tool].ainvoke(dict(type='tool_call',id=call,name=tool,args=args))
        import json
        return json.loads(message.content)
    async def run():
        req = dict(argv=['rg','REMOTE','.'])
        for _ in range(2):
            result = await invoke('run_command',req,'search')
            assert result['execution_status']=='succeeded',result
            assert 'REMOTE' in result['stdout_tail']
        written = await invoke('run_python', {
            'mode': 'workspace_rw',
            'code': "from pathlib import Path\nPath('live.txt').write_text('live')",
        }, 'live-write')
        assert written['execution_status'] == 'succeeded', written
        assert written['publish_status'] == 'not_requested'
        assert (project/'live.txt').read_text() == 'live'
        read = await invoke('run_python', {
            'mode': 'workspace_ro', 'output': None,
            'code': "from pathlib import Path\nprint(Path('live.txt').read_text())",
        }, 'live-read')
        assert read['execution_status'] == 'succeeded', read
        assert read['stdout_tail'].strip() == 'live'
        with pytest.raises(ValueError, match='请省略 output'):
            await invoke('run_python', {
                'mode': 'workspace_rw', 'output': {'target': 'reports'}, 'code': 'pass',
            }, 'invalid-output')
        staged = await invoke('run_python',dict(code="from pathlib import Path\nimport os\n(Path(os.environ['ARTORIAS_OUTPUT_DIR'])/'x.txt').write_text('ok')",
                                     output={'target':'reports'}),'staged')
        assert staged['publish_status']=='succeeded',staged
        assert (project/'reports/x.txt').read_text()=='ok'
        assert len(approvals)==4
        assert not (tmp_path/'logical-only').exists()
    asyncio.run(run())


def test_stale_entry_and_directory_escape(tmp_path, monkeypatch):
    project = tmp_path/'project'
    project.mkdir()
    (project/'script.py').write_text('print(1)')
    runner = WorkspaceRunner(tmp_path/'state',lambda _:project)
    if sys.platform.startswith('linux'):
        runner.sandbox = 'bubblewrap'
        monkeypatch.setattr('artorias_device.tools.workspace_runner.shutil.which',lambda x:'/usr/bin/'+x)
    async def scenario():
        work = job(dict(mode='workspace_ro',path='script.py'))
        plan = await runner.prepare(work)
        (project/'script.py').write_text('print(2)')
        with pytest.raises(ValueError,match='变化'):
            await runner.start(work.execution_id,plan['plan_hash'])
        (project/'outside').symlink_to(tmp_path)
        with pytest.raises(ValueError,match='符号链接'):
            await runner.prepare(job(dict(mode='workspace_rw',working_directory='outside',code='pass'),'b'))
    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform!='darwin',reason='real runner RPC and Seatbelt')
def test_separate_runner_process_restart(tmp_path):
    import json
    import os
    import secrets
    import socket
    import subprocess
    import time
    import httpx
    from artorias_server.tools.workspace_execution import RunnerClient

    root = tmp_path/'project'
    root.mkdir()
    (root/'marker').write_text('owner-host')
    token = secrets.token_hex(32)
    config = tmp_path/'runner.json'
    config.write_text(json.dumps(dict(kind='device_fs',location_id='mac',data=str(tmp_path/'state'),
                         workspaces={'project':str(root)},token_env='TEST_RUNNER_TOKEN')))
    env = dict(os.environ,ARTORIAS_RUNNER_CONFIG=str(config),TEST_RUNNER_TOKEN=token)
    work = job(dict(code="from pathlib import Path\np=Path('marker')\np.write_text(p.read_text()+'!')",
                    mode='workspace_rw'))
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0))
        listener.listen()
        url = 'http://127.0.0.1:'+str(listener.getsockname()[1])
        client = RunnerClient(url,token)
        for attempt in range(2):
            process = subprocess.Popen([sys.executable,'-m','uvicorn','artorias_local_app.runner:create_app',
                              '--factory','--fd',str(listener.fileno()),'--log-level','warning'],
                              pass_fds=(listener.fileno(),),env=env)
            try:
                with httpx.Client(base_url=url,headers=client.headers,timeout=.2,trust_env=False) as http:
                    for _ in range(100):
                        try:
                            if http.get('/capabilities').is_success:
                                break
                        except httpx.TransportError:
                            pass
                        assert process.poll() is None
                        time.sleep(.05)
                async def execute():
                    plan = await client.prepare(work)
                    result = await client.start(work.execution_id,plan['plan_hash'])
                    for _ in range(200):
                        if result['execution_status']!='running':
                            break
                        await asyncio.sleep(.05)
                        result = await client.status(work.execution_id)
                    assert result['execution_status']=='succeeded',result
                asyncio.run(execute())
                assert (root/'marker').read_text()=='owner-host!'
            finally:
                process.terminate()
                process.wait(timeout=10)


def test_remote_runtime_starts_without_device_filesystem(tmp_path,monkeypatch):
    import json
    from artorias_server.remote_app import create_app
    routes = tmp_path/'routes.json'
    routes.write_text(json.dumps([dict(kind='device_fs',location_id='mac',url='http://127.0.0.1:18766',
                                       token_env='TEST_RUNNER_TOKEN')]))
    monkeypatch.setenv('ARTORIAS_RUNNER_ROUTES',str(routes))
    monkeypatch.setenv('TEST_RUNNER_TOKEN','x'*32)
    monkeypatch.setenv('ARTORIAS_DEVICE_ID','mac')
    monkeypatch.setenv('ARTORIAS_DATA',str(tmp_path/'runtime'))
    monkeypatch.setenv('ARTORIAS_SEARCH_MODE','embedded')
    with TestClient(create_app()) as client:
        headers = {'x-artorias-token':client.get('/bootstrap').json()['token']}
        session = client.post('/api/sessions',headers=headers).json()
        assert session['id']
        assert not (tmp_path/'runtime'/'workspace-runner').exists()


def test_file_mutations_respect_live_workspace_lease(tmp_path,monkeypatch):
    import fcntl
    from artorias_shared.execution.workspace import digest
    from artorias_local_app.bootstrap import create_workspace
    from artorias_server.storage.sqlite import Store
    root = tmp_path/'project'
    root.mkdir()
    monkeypatch.setenv('ARTORIAS_WORKSPACE',str(root))
    data = tmp_path/'runtime'
    data.mkdir()
    db = Store(data/'app.sqlite')
    ws = create_workspace(db,db.session())
    ws.write('data.txt','before',expected=None)
    gate = data/'workspace-runner'/('lock-'+digest(str(root.resolve())))
    with gate.open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        with pytest.raises(ValueError,match='执行结束后重试'):
            ws.write('data.txt','concurrent')
    assert ws.read('data.txt')=='before'
    ws.write('data.txt','after')
    assert ws.read('data.txt')=='after'
