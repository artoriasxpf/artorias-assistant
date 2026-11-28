import asyncio
import json
from uuid import uuid4

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from artorias_local_app.bootstrap import create_engine, create_workspace
from artorias_server.scheduler.service import Scheduler
from artorias_server.storage.sqlite import Store
from artorias_server.tasks.executor import TaskRuntime
from artorias_server.tasks.models import Plan
from artorias_server.work.repository import resume
from task_fixtures import finish_message, plan_item


class PartitionModel(GenericFakeChatModel):
    detailed_notes: bool = False
    premature_read: bool = False
    stop_after_first_page: bool = False
    oversized_reduction: bool = False

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, *args, **kwargs):
        if isinstance(messages[-1], HumanMessage) and messages[-1].text.startswith(
            "只调用一次 finish_work_unit"
        ):
            assert "9000 字节" in messages[-1].text
            assert "完整尾部" in messages[0].text
            message = AIMessage(
                content="",
                tool_calls=[
                    dict(
                        name="finish_work_unit",
                        id=uuid4().hex,
                        args={"summary": "已重新综合"},
                    )
                ],
            )
            return ChatResult(generations=[ChatGeneration(message=message)])
        snapshot = next(
            json.loads(m.content.split("\n", 1)[1])
            for m in messages
            if isinstance(m, SystemMessage)
            and isinstance(m.content, str)
            and m.content.startswith("以下为已保存的只读分析数据")
        )
        system = next(
            m.text for m in messages if isinstance(m, SystemMessage) and "你是 Artorias" in m.text
        )
        raw = system[system.index('{"unit":') :]
        spec = json.JSONDecoder().raw_decode(raw)[0]
        kind = spec["unit"]
        if (
            self.stop_after_first_page
            and kind == "read_group"
            and not snapshot["pending"]
            and snapshot["committed_batches"] == 1
        ):
            retry = next(
                (
                    m.text
                    for m in messages
                    if isinstance(m, SystemMessage) and m.text.startswith("读取分组尚未覆盖")
                ),
                None,
            )
            if retry is None:
                return ChatResult(
                    generations=[ChatGeneration(message=AIMessage(content="第一页已读，还需续读。"))]
                )
            args = json.loads(retry.split("\n", 1)[1])
            span = snapshot["file_ranges"]["large.md"]
            assert args == dict(
                path="large.md", unit="byte", limit=4096, offset=4096, expected_version=span["version"]
            )
        for dependency in spec["dependencies"]:
            if "notes" in dependency and self.detailed_notes:
                assert all(
                    finding["text"].endswith("完整尾部")
                    for item in dependency["notes"]
                    for finding in item["note"]["findings"]
                )
                assert all(
                    item["sources"][0]["requested_path"] == "large.md" for item in dependency["notes"]
                )
        if snapshot["pending"]:
            data = snapshot["pending"]
            last_tool = next((m for m in reversed(messages) if isinstance(m, ToolMessage)), None)
            if (
                kind == "read_group"
                and self.premature_read
                and last_tool
                and last_tool.name == "project_read"
            ):
                content = str(last_tool.content)
                if last_tool.status != "error" and data["id"] in content:
                    result = json.loads(content)
                    if result.get("next_start") is not None:
                        args = dict(
                            json.loads(data["plan_json"])["args"],
                            offset=result["next_start"],
                            expected_version=result["version"],
                        )
                        message = AIMessage(
                            content="",
                            tool_calls=[dict(name="project_read", args=args, id=uuid4().hex)],
                        )
                        return ChatResult(generations=[ChatGeneration(message=message)])
            if data["state"] == "presented" or any(
                isinstance(m, ToolMessage) and data["id"] in str(m.content) for m in messages
            ):
                name, args = "record_analysis_note", dict(batch_id=data["id"], summary="测试证据")
                if self.detailed_notes:
                    args["findings"] = [{"text": "详细结论" * 110 + "完整尾部", "refs": [0]}] * 4
            else:
                request = json.loads(data["plan_json"])
                name, args = request["tool"], request["args"]
        elif kind == "inventory" and (not snapshot["committed_batches"] or snapshot["continuations"]):
            name, args = "project_inventory", dict(root=spec["inputs"]["paths"][0], snapshot_page=True)
            if snapshot["continuations"]:
                args["cursor"] = snapshot["continuations"][-1]["next_cursor"]
        elif kind == "inventory":
            pytest.fail("completed inventory should advance without another model call")
        elif kind == "read_group":
            paths = spec["inputs"]["paths"]
            ranges = snapshot["file_ranges"]
            path = next(
                (
                    p
                    for p in paths
                    if not ranges[p].get("skipped")
                    and ranges[p]["total"] != ranges[p]["offset"]
                    and (len(paths) > 1 or ranges[p]["offset"] - spec["inputs"].get("start", 0) < 32768)
                ),
                None,
            )
            if path:
                span = ranges[path]
                name, args = (
                    "project_read",
                    dict(path=path, unit="byte", limit=4096, offset=span["offset"]),
                )
                if span["version"]:
                    args["expected_version"] = span["version"]
            else:
                pytest.fail("completed read group should advance without a summary tool call")
        elif kind == "synthesis":
            assert spec["dependencies"] and "full" in spec["coverage"]
            return ChatResult(generations=[ChatGeneration(message=finish_message("汇总完成"))])
        else:
            summary = "已核验清单"
            if self.oversized_reduction:
                sequences = [
                    note["seq"] for dep in spec["dependencies"] for note in dep.get("notes", [])
                ]
                summary = "摘" * (3000 if 1 in sequences else 1662)
            name, args = "finish_work_unit", dict(summary=summary)
        msg = AIMessage(content="", tool_calls=[dict(name=name, args=args, id=uuid4().hex)])
        return ChatResult(generations=[ChatGeneration(message=msg)])


@pytest.mark.parametrize("cloud", [False, True], ids=["local", "managed-cloud"])
@pytest.mark.parametrize("call_limit", [2, 6])
def test_partition_resume_and_deliver_once(tmp_path, monkeypatch, cloud, call_limit):
    monkeypatch.setenv("ARTORIAS_WORKSPACE", str(tmp_path / "project"))
    monkeypatch.setenv("ARTORIAS_WORK_MODEL_CALLS", str(call_limit))
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    db = Store(data / "app.sqlite")
    sid = db.session()
    if cloud:
        from artorias_server.workspace.catalog import WorkspaceCatalog

        catalog = WorkspaceCatalog(db)
        catalog.select(sid, catalog.register("云端", "reports", "cloud_storage")["id"])
    original = db.submit(sid, "分析目录", "initial")
    ws = create_workspace(db, sid, original["id"])
    ws.write("docs/a.md", "a")
    ws.write("docs/b.md", "b")
    planned = []

    async def planner(_):
        planned.append(True)
        return Plan(
            kind="tasks",
            items=[
                plan_item(
                    key="summary",
                    goal="分析目录",
                    analysis=dict(
                        analysis_type="document_directory",
                        goal="汇总",
                        intent="summary",
                        roots=["docs"],
                        coverage_policy="full",
                    ),
                )
            ],
        )

    def execute(store):
        model = PartitionModel(messages=iter([]), disable_streaming=True)
        runtime = TaskRuntime(
            data, store, engine=create_engine(data, store, model_factory=lambda: model), planner=planner
        )
        scheduler = Scheduler(store, runtime)
        for _ in range(16):
            run = store.claim()
            if not run:
                return
            asyncio.run(scheduler.execute(run))
        pytest.fail("unexpected scheduling loop")

    execute(db)
    plan = db.query("SELECT * FROM work_plans")[0]
    assert plan["status"] == "waiting", db.query(
        "SELECT kind,payload FROM events WHERE kind LIKE '%.failed'"
    )
    assert db.query("SELECT 1 FROM events WHERE kind='task.delivered'") == []
    assert db.query("SELECT count(*) n FROM analysis_notes")[0]["n"] == (1 if call_limit == 2 else 3)
    if call_limit == 6:
        group = db.query("SELECT status,summary FROM work_units WHERE kind='read_group'")[0]
        assert group["status"] == "completed"
        assert group["summary"].count("测试证据") == 2
    assert db.query("SELECT status FROM work_units WHERE kind='inventory'") == [{"status": "completed"}]
    assert not db.query(
        "SELECT 1 FROM events WHERE kind='tool.call' AND json_extract(payload,'$.name')='finish_work_unit'"
    )
    db = Store(data / "app.sqlite")
    db.recover()
    with pytest.raises(ValueError, match="追加"):
        db.submit(sid, "继续", "text")
    workspace_id = ws.describe()["id"]
    with db.tx() as c:
        for _ in range(2):
            resumed = resume(
                c,
                sid,
                "追加 40 次模型调用并继续",
                "retry",
                workspace_id,
                plan["id"],
                plan["generation"],
                40,
            )
            assert resumed["id"] == original["id"]
    execute(db)
    assert db.query("SELECT status FROM runs") == [{"status": "completed"}], db.query(
        "SELECT kind,payload FROM events WHERE kind LIKE '%.failed' OR kind='work.plan.waiting'"
    )
    assert [r["kind"] for r in db.query("SELECT kind FROM work_units ORDER BY ordinal")] == [
        "inventory",
        "read_group",
        "synthesis",
    ]
    assert len(planned) == 1
    assert len(db.query("SELECT 1 FROM events WHERE kind='task.delivered'")) == 1
    assert (
        len(
            db.query(
                "SELECT 1 FROM events WHERE kind='tool.call' AND json_extract(payload,'$.name')='project_inventory'"
            )
        )
        == 1
    )


def test_multiple_pages_ranges_reduction_and_changed_source(tmp_path, monkeypatch):
    root, data = tmp_path / "project", tmp_path / "data"
    root.mkdir()
    data.mkdir()
    monkeypatch.setenv("ARTORIAS_WORKSPACE", str(root))
    monkeypatch.setenv("ARTORIAS_WORK_MODEL_CALLS", "300")
    (root / "docs").mkdir()
    # Seven read groups trigger a bounded intermediate reduction. Extra small files
    # force a second inventory page. The final UTF-8 character straddles a byte page.
    for i in range(6):
        (root / "docs" / f"{i}.md").write_text("a" * 17000)
    (root / "docs" / "big.md").write_text("x" * 32767 + "汉字")
    for name in ("x", "y", "z"):
        (root / "docs" / name).write_text(name)
    (root / "docs" / "binary").write_bytes(b"abc\x00def")
    store = Store(data / "app.sqlite")
    sid = store.session()
    store.submit(sid, "完整阅读目录", "first")

    async def planner(_):
        return Plan(
            kind="tasks",
            items=[
                plan_item(
                    key="scan",
                    goal="完整阅读",
                    analysis=dict(
                        analysis_type="document_directory",
                        goal="汇总",
                        intent="summary",
                        roots=["docs"],
                        coverage_policy="full",
                    ),
                )
            ],
        )

    model = PartitionModel(messages=iter([]), disable_streaming=True)
    runtime = TaskRuntime(
        data, store, engine=create_engine(data, store, model_factory=lambda: model), planner=planner
    )
    scheduler = Scheduler(store, runtime)
    changed = False
    for _ in range(40):
        run = store.claim()
        if not run:
            break
        if not changed and store.query("SELECT 1 FROM work_units WHERE kind='synthesis'"):
            (root / "docs" / "x").write_text("new")
            changed = True
        asyncio.run(scheduler.execute(run))
    assert store.query("SELECT status FROM runs") == [{"status": "completed"}], store.query(
        "SELECT kind,payload FROM events WHERE kind IN ('run.failed','work.plan.waiting')"
    )
    assert changed and store.query(
        "SELECT 1 FROM work_units WHERE kind='reduce' AND status='completed'"
    )
    reads = store.query("""SELECT json_extract(payload,'$.args.path') path FROM events WHERE kind='tool.call'
                           AND json_extract(payload,'$.name')='project_read'""")
    assert sum(r["path"] == "docs/x" for r in reads) == 2
    assert sum(r["path"] == "docs/y" for r in reads) == 1
    assert len(store.query("SELECT 1 FROM events WHERE kind='work.source.changed'")) == 1
    final = json.loads(
        store.query("SELECT payload FROM events WHERE kind='analysis.result'")[-1]["payload"]
    )
    assert final["coverage"]["fully_read_files"] == 10 and not final["coverage"]["full"]
    assert final["partial"] and final["coverage"]["skipped_files"] == 1


@pytest.mark.parametrize("cancel", [False, True])
def test_two_waiting_steps_require_explicit_selection(tmp_path, monkeypatch, cancel):
    data, root = tmp_path / "data", tmp_path / "project"
    data.mkdir()
    root.mkdir()
    (root / "one").write_text("1")
    (root / "two").write_text("2")
    monkeypatch.setenv("ARTORIAS_WORKSPACE", str(root))
    monkeypatch.setenv("ARTORIAS_WORK_MODEL_CALLS", "1")
    store = Store(data / "app.sqlite")
    sid = store.session()
    run = store.submit(sid, "分析两个文件", "start")

    async def planner(_):
        return Plan(
            kind="tasks",
            items=[
                plan_item(
                    key=p,
                    goal=p,
                    analysis=dict(
                        analysis_type="document_file",
                        goal=p,
                        intent="summary",
                        roots=[p],
                        coverage_policy="full",
                    ),
                )
                for p in ("one", "two")
            ],
        )

    def drain():
        model = PartitionModel(messages=iter([]), disable_streaming=True)
        runtime = TaskRuntime(
            data, store, engine=create_engine(data, store, model_factory=lambda: model), planner=planner
        )
        scheduler = Scheduler(store, runtime)
        for _ in range(12):
            next_run = store.claim()
            if not next_run:
                return scheduler
            asyncio.run(scheduler.execute(next_run))
        pytest.fail("run did not park")

    scheduler = drain()
    plans = store.query("SELECT * FROM work_plans ORDER BY step_key")
    assert [p["status"] for p in plans] == ["waiting", "waiting"]
    with pytest.raises(ValueError, match="请选择"):
        store.submit(sid, "继续", "ambiguous")
    from artorias_server.workspace.catalog import WorkspaceCatalog

    workspace = WorkspaceCatalog(store).current(sid)["id"]
    with store.tx() as db:
        resume(db, sid, "继续第一个", "one-key", workspace, plans[0]["id"], plans[0]["generation"], 8)
    scheduler = drain()
    assert store.query("SELECT status FROM work_plans ORDER BY step_key") == [
        {"status": "completed"},
        {"status": "waiting"},
    ]
    if not cancel:
        with store.tx() as db:
            resume(
                db, sid, "继续第二个", "two-key", workspace, plans[1]["id"], plans[1]["generation"], 8
            )
        drain()
        assert store.query("SELECT status FROM runs") == [{"status": "completed"}]
        assert len(store.query("SELECT 1 FROM events WHERE kind='task.delivered'")) == 2
        return
    scheduler.cancel(run["id"])
    with store.tx() as db, pytest.raises(ValueError, match="恢复或结束"):
        resume(db, sid, "继续第二个", "two-key", workspace, plans[1]["id"], plans[1]["generation"], 8)


def test_full_notes_are_partitioned_without_truncation(tmp_path, monkeypatch):
    root, data = tmp_path / "project", tmp_path / "data"
    root.mkdir()
    data.mkdir()
    monkeypatch.setenv("ARTORIAS_WORKSPACE", str(root))
    (root / "large.md").write_text("x" * 20480)
    store = Store(data / "app.sqlite")
    sid = store.session()
    store.submit(sid, "通读并汇总", "first")

    async def planner(_):
        return Plan(
            kind="tasks",
            items=[
                plan_item(
                    key="scan",
                    goal="汇总",
                    analysis=dict(
                        analysis_type="document_file",
                        goal="汇总",
                        intent="summary",
                        roots=["large.md"],
                        coverage_policy="full",
                    ),
                )
            ],
        )

    model = PartitionModel(
        messages=iter([]),
        disable_streaming=True,
        detailed_notes=True,
        oversized_reduction=True,
        premature_read=True,
        stop_after_first_page=True,
    )
    runtime = TaskRuntime(
        data, store, engine=create_engine(data, store, model_factory=lambda: model), planner=planner
    )
    scheduler = Scheduler(store, runtime)
    for _ in range(12):
        run = store.claim()
        if not run:
            break
        asyncio.run(scheduler.execute(run))
    assert store.query("SELECT status FROM runs") == [{"status": "completed"}]
    groups = store.query("SELECT inputs FROM work_units WHERE kind='reduce' AND status='completed'")
    assert len(groups) == 2
    sequences = [
        number
        for group in groups
        for sequence in json.loads(group["inputs"])["note_sequences"].values()
        for number in sequence
    ]
    assert sequences == [1, 2, 3, 4, 5]
    errors = store.query("""SELECT json_extract(payload,'$.content') AS content FROM events
        WHERE kind='tool.result' AND json_extract(payload,'$.name')='project_read'
        AND json_extract(payload,'$.status')='error'""")
    assert len(errors) == 4
    assert all(
        "record_analysis_note" in row["content"]
        and "batch_id=" in row["content"]
        and "offset=" not in row["content"]
        for row in errors
    )
    assert store.query("SELECT count(*) AS count FROM analysis_batches") == [{"count": 5}]
    summaries = store.query("SELECT summary FROM work_units WHERE kind='reduce' ORDER BY ordinal")
    assert [len(row["summary"].encode()) for row in summaries] == [len("已重新综合".encode()), 4986]
    assert (
        len(
            store.query("""SELECT 1 FROM events WHERE kind='model.started'
        AND json_extract(payload,'$.stage')='work_summary_repair'""")
        )
        == 1
    )


def test_named_document_uses_prior_user_directory_with_read_approval(tmp_path, monkeypatch):
    data, current = tmp_path / 'app', tmp_path / 'current'
    data.mkdir()
    current.mkdir()
    prior = tmp_path / 'previous-project' / 'agent-docs'
    target = prior / 'design' / 'task_design.md'
    target.parent.mkdir(parents=True)
    target.write_text('目标文档正文', encoding='utf-8')
    monkeypatch.setenv('ARTORIAS_WORKSPACE', str(current))
    store = Store(data / 'app.sqlite')
    sid = store.session()
    store.submit(sid, f'总结这个目录：{prior}', 'previous')
    first = store.claim()
    store.finish(first, 'completed', '已完成目录总结')
    store.submit(sid, '帮我精读 task_design', 'new')

    async def planner(_):
        return Plan(kind='tasks', items=[
            plan_item('read', '通读设计文档', analysis={
                'analysis_type': 'document_file', 'goal': '通读', 'intent': '解释',
                'roots': ['agent-docs/design/task_design.md'], 'coverage_policy': 'full',
            }),
        ])

    model = PartitionModel(messages=iter([]), disable_streaming=True)
    runtime = TaskRuntime(data, store, engine=create_engine(data, store, model_factory=lambda: model),
                          planner=planner)
    scheduler = Scheduler(store, runtime)

    async def scenario():
        run = store.claim()
        rid = run['id']
        await scheduler.execute(run)
        requests = store.query("SELECT seq,payload FROM events WHERE run=? AND kind='approval.requested'", (rid,))
        assert len(requests) == 1
        request = json.loads(requests[0]['payload'])['requests'][0]
        assert (request['path'], request['access']) == (str(prior), 'read')
        assert store.query("SELECT kind,status FROM work_units") == [
            {'kind': 'locate_file', 'status': 'running'}
        ]
        store.decide(rid, {request['request_id']: 'approve'}, requests[0]['seq'])
        for _ in range(8):
            step_run = store.claim()
            if not step_run:
                break
            await scheduler.execute(step_run)
        rows = store.query("SELECT payload FROM events WHERE run=? AND kind='tool.call'", (rid,))
        reads = [json.loads(row['payload']) for row in rows]
        assert [item['args']['path'] for item in reads if item['name']=='project_read'] == [str(target)]
        assert store.query("SELECT status FROM runs WHERE id=?", (rid,))[0]['status'] == 'completed'

    asyncio.run(scenario())


def test_document_missing_during_read_is_relocated_once(tmp_path, monkeypatch):
    data, root = tmp_path / 'app', tmp_path / 'project'
    data.mkdir()
    root.mkdir()
    original = root / 'task_design.md'
    original.write_text('文档正文', encoding='utf-8')
    replacement = root / 'archived' / original.name
    replacement.parent.mkdir()
    monkeypatch.setenv('ARTORIAS_WORKSPACE', str(root))
    store = Store(data / 'app.sqlite')
    sid = store.session()
    store.submit(sid, '精读 task_design.md', 'start')

    async def planner(_):
        return Plan(kind='tasks', items=[
            plan_item('read', '通读文档', analysis={
                'analysis_type': 'document_file', 'goal': '通读', 'intent': '解释',
                'roots': ['task_design.md'], 'coverage_policy': 'full',
            }),
        ])

    class MovingModel(PartitionModel):
        def _generate(self, messages, *args, **kwargs):
            response = super()._generate(messages, *args, **kwargs)
            if original.exists() and any(call['name'] == 'project_read'
                                         for call in response.generations[0].message.tool_calls):
                original.rename(replacement)
            return response

    model = MovingModel(messages=iter([]), disable_streaming=True)
    runtime = TaskRuntime(data, store, engine=create_engine(data, store, model_factory=lambda: model),
                          planner=planner)
    scheduler = Scheduler(store, runtime)

    async def scenario():
        for _ in range(10):
            run = store.claim()
            if not run:
                return
            await scheduler.execute(run)

    asyncio.run(scenario())
    rows = store.query("SELECT payload FROM events WHERE kind='tool.call' AND json_extract(payload,'$.name')='project_read'")
    assert [json.loads(row['payload'])['args']['path'] for row in rows] == [
        'task_design.md', 'archived/task_design.md',
    ]
    assert store.query("SELECT status FROM runs")[0]['status'] == 'completed'
    units = store.query("SELECT status FROM work_units WHERE kind='read_group' ORDER BY ordinal")
    assert units == [{'status': 'obsolete'}, {'status': 'completed'}]
    assert store.query("SELECT status FROM work_units WHERE kind='locate_file'") == [
        {'status': 'completed'}
    ]


@pytest.mark.parametrize('outcome', ['approve', 'reject', 'ambiguous'])
def test_file_location_work_unit_searches_after_directory_move(tmp_path, monkeypatch, outcome):
    data, projects = tmp_path / 'app', tmp_path / 'projects'
    current = projects / 'current'
    current.mkdir(parents=True)
    data.mkdir()
    previous = projects / 'old-project' / 'agent-docs'
    target = projects / 'renamed-project' / 'agent-docs' / 'design' / 'task_workplan.md'
    target.parent.mkdir(parents=True)
    target.write_text('目标文档正文', encoding='utf-8')
    duplicate = projects / 'other-project' / target.name
    if outcome == 'ambiguous':
        duplicate.parent.mkdir()
        duplicate.write_text('另一份文档', encoding='utf-8')
    monkeypatch.setenv('ARTORIAS_WORKSPACE', str(current))
    store = Store(data / 'app.sqlite')
    sid = store.session()
    store.submit(sid, f'总结这个目录：{previous}', 'previous')
    store.finish(store.claim(), 'completed', '已完成目录总结')
    store.submit(sid, f'精读 {target.name}', 'new')

    async def planner(_):
        return Plan(kind='tasks', items=[
            plan_item('read', '通读设计文档', analysis={
                'analysis_type': 'document_file', 'goal': '通读', 'intent': '解释',
                'roots': [target.name], 'coverage_policy': 'full',
            }),
        ])

    class LocationModel(PartitionModel):
        def _generate(self, messages, *args, **kwargs):
            context = next(
                (m.text for m in messages if isinstance(m, SystemMessage)
                 and m.text.startswith('定位数据见以下 JSON。')),
                None,
            )
            if context is None:
                return super()._generate(messages, *args, **kwargs)
            observations = json.loads(context.split('。', 1)[1])
            assert observations['reference'] == target.name
            assert str(projects) in observations['suggested_roots']
            recent_search = next(
                (m for m in reversed(messages) if isinstance(m, ToolMessage)
                 and m.name == 'search_for_file'),
                None,
            )
            if recent_search is None:
                name, arguments = 'search_for_file', {'root': str(projects)}
            else:
                result = json.loads(recent_search.content)
                assert result['status'] == ('denied' if outcome == 'reject' else 'searched')
                name = 'finish_file_location'
                candidates = result.get('candidates', [])
                expected = {
                    'approve': {str(target)}, 'reject': set(),
                    'ambiguous': {str(target), str(duplicate)},
                }[outcome]
                assert set(candidates) == expected
                # An invented path, or a guess from ambiguous evidence, must fail.
                last = messages[-1]
                if isinstance(last, ToolMessage) and last.name == 'finish_file_location':
                    assert last.status == 'error'
                    arguments = {'path': candidates[0]} if len(candidates) == 1 else {}
                else:
                    arguments = {'path': str(target) if len(candidates) > 1 else str(target) + '.bak'}
            message = AIMessage(content='', tool_calls=[dict(name=name, args=arguments, id=uuid4().hex)])
            return ChatResult(generations=[ChatGeneration(message=message)])

    def scheduler():
        model = LocationModel(messages=iter([]), disable_streaming=True)
        return Scheduler(store, TaskRuntime(data, store, engine=create_engine(
            data, store, model_factory=lambda: model), planner=planner))

    async def scenario():
        run = store.claim()
        rid = run['id']
        await scheduler().execute(run)
        for expected_path, decision in [(previous, 'approve'), (projects, 'reject' if outcome == 'reject' else 'approve')]:
            requests = store.query(
                "SELECT seq,payload FROM events WHERE run=? AND kind='approval.requested' ORDER BY seq",
                (rid,),
            )
            latest = requests[-1]
            (request,) = json.loads(latest['payload'])['requests']
            assert (request['path'], request['access']) == (str(expected_path), 'read')
            assert store.query("SELECT kind,status FROM work_units") == [
                {'kind': 'locate_file', 'status': 'running'}
            ]
            assert not store.query(
                "SELECT 1 FROM events WHERE run=? AND kind='tool.call' AND json_extract(payload,'$.name')='project_read'",
                (rid,),
            )
            store.decide(rid, {request['request_id']: decision}, latest['seq'])
            await scheduler().execute(store.claim())

        for _ in range(8):
            pending = store.claim()
            if not pending:
                break
            await scheduler().execute(pending)
        assert store.query('SELECT status FROM runs WHERE id=?', (rid,)) == [{'status': 'completed'}]
        units = store.query('SELECT id,kind,status,inputs,dependencies FROM work_units ORDER BY ordinal')
        locate = units[0]
        assert locate['kind'] == 'locate_file' and locate['status'] == 'completed'
        state = json.loads(locate['inputs'])
        assert state['searches'][0]['status'] == ('denied' if outcome == 'reject' else 'searched')
        assert len(store.query(
            "SELECT 1 FROM events WHERE run=? AND kind='approval.requested'", (rid,),
        )) == 2
        if outcome == 'approve':
            assert [u['kind'] for u in units] == ['locate_file', 'read_group', 'synthesis']
            assert json.loads(units[1]['inputs'])['paths'] == [str(target)]
            assert json.loads(units[1]['dependencies']) == [locate['id']]
        else:
            assert len(units) == 1
            error = state['selection']['error']
            if outcome == 'ambiguous':
                assert str(target) in error and str(duplicate) in error
            else:
                assert '未获读取权限' in error
                assert str(target) not in error

    asyncio.run(scenario())
