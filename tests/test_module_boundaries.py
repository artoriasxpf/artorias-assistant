"""Behavioral checks for replaceable execution backends, without remote services."""
import asyncio
from contextlib import asynccontextmanager
from functools import partial
import pytest
from langchain_core.messages import AIMessage
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from artorias_server.storage.sqlite import Store
from artorias_server.runtime.service import DeepAgentRuntime
from artorias_server.runtime.checkpoints import SQLiteCheckpointProvider
from artorias_server.tools.registry import build_tools
from artorias_server.tools.files import FileToolService

class Model(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self

class MemoryWorkspace:
    """A non-filesystem implementation with the same resource interface."""
    def __init__(self):
        self.content = {'a.py': 'print(42)'}
        self.versions = {'a.py': 'v1'}
    def describe(self):return {'id': 'remote-test', 'kind': 'cloud_storage'}
    def path(self, path):return 'remote-test:' + path
    def files(self):return list(self.content)
    def read_version(self, path):return self.content[path], self.versions[path]
    def read_bytes(self, path):return self.content[path].encode()
    def head(self, path):return self.versions.get(path)
    def write(self, path, content, *, expected):
        if expected != self.head(path):raise ValueError('conflict')
        self.content[path] = content
        self.versions[path] = self.versions.get(path, '') + '+'
        return {'version': self.versions[path]}
    def delete(self, path):
        del self.content[path];del self.versions[path];return 'deleted'

class Checkpoints:
    def __init__(self, path):
        self.local = SQLiteCheckpointProvider(path)
        self.opened = 0
    @asynccontextmanager
    async def open(self):
        self.opened += 1
        async with self.local.open() as saver:yield saver

class Sandbox:
    def __init__(self):self.calls = []
    async def run_python(self, workspace, path):
        self.calls.append((workspace, path))
        return {'exit_code': 0, 'output': '42'}

def test_injected_backends_survive_approval_resume(tmp_path):
    db = Store(tmp_path/'app.sqlite');sid = db.session()
    db.submit(sid, 'run script', 'one');run = db.claim()
    ws = MemoryWorkspace();sandbox = Sandbox();cp = Checkpoints(tmp_path/'injected.sqlite')
    model = Model(disable_streaming=True, messages=iter([
        AIMessage(content='', tool_calls=[{'name':'run_python','args':{'path':'a.py'},'id':'c1','type':'tool_call'}]),
        AIMessage(content='finished'),
    ]))
    factory_calls = []
    def factory(r):factory_calls.append(r['id']);return ws
    engine = DeepAgentRuntime(tmp_path, db, workspace_factory=factory,
        model_factory=lambda:model, checkpoint_provider=cp,
        tool_factory=partial(build_tools, sandbox=sandbox))
    async def collect(r):return [e async for e in engine.execute(r)]
    first = asyncio.run(collect(run))
    assert first[-1][0] == 'approval.requested'
    assert sandbox.calls == []
    db.finish(run, 'waiting');db.decide(run['id'], 'approve')
    second = asyncio.run(collect(db.claim()))
    assert second[-1] == ('assistant.final', {'text':'finished'})
    assert sandbox.calls == [(ws, 'a.py')]
    assert cp.opened == 2 and factory_calls == [run['id'], run['id']]
    assert not (tmp_path/'checkpoints.sqlite').exists()

def test_file_tool_baselines_are_not_silently_replaced():
    ws = MemoryWorkspace();service = FileToolService(ws)
    service.read('a.py')
    ws.write('a.py', 'other task', expected='v1')
    with pytest.raises(ValueError, match='conflict'):service.write('a.py','stale change')
    recreated = FileToolService(ws)
    with pytest.raises(ValueError, match='project_read'):recreated.write('a.py','blind overwrite')
    recreated.read('a.py');recreated.write('a.py','fresh change')
    assert ws.content['a.py'] == 'fresh change'
