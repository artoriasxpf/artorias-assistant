import asyncio
import json

import pytest

from artorias_server.scheduler.service import Scheduler
from artorias_server.storage.sqlite import Store
from artorias_server.tasks.executor import TaskRuntime
from artorias_server.tasks.models import Claim, Wait
from artorias_server.tasks.reviews import Decision, decide
from task_fixtures import plan_item, submission, user_rule
from artorias_server.tasks.models import Plan


def review_run(tmp_path):
    store = Store(tmp_path / "app.sqlite")
    session = store.session()
    store.submit(session, "预览确认后再继续", "start")
    run = store.claim()
    preview = plan_item("preview", "版式预览")
    preview.step_spec.local_reviews["layout"] = user_rule("确认此预览？")
    final = plan_item("final", "全量结果", depends_on=["preview"])
    final.step_spec.wait_for = [Wait(step_key="preview", criterion_ref="layout")]
    independent = plan_item("other", "其他工作", criteria=["明确签收"])
    calls = []

    async def planner(payload):
        return Plan(kind="tasks", items=[preview, final, independent])

    class Engine:
        async def execute(self, scoped):
            key = scoped["task_step_id"]
            calls.append(key)
            result = submission(key)
            if key == "preview":
                result.criterion_claims = [
                    Claim(id="layout", claim="met", reason="预览已交付")
                ]
            elif key == "other":
                result.criterion_claims = [Claim(id="C1", claim="met", reason="等待签收")]
            yield "step.submitted", result.model_dump()

    runtime = TaskRuntime(tmp_path, store, Engine(), planner)
    scheduler = Scheduler(store, runtime)
    asyncio.run(scheduler.execute(run))
    pending = store.query("""SELECT r.id,a.criterion_version,a.evidence_version FROM task_review_requests r
        JOIN task_criterion_assessments a ON a.id=r.assessment_id WHERE a.criterion_id='layout'""")[
        0
    ]
    return store, scheduler, run, pending, calls


@pytest.mark.parametrize("choice", ["accept", "request_changes"])
def test_review_gate_survives_restart_and_repeated_decision(tmp_path, choice):
    store, scheduler, run, review, calls = review_run(tmp_path)
    assert calls == ["preview", "other"]
    assert store.query("SELECT status FROM runs")[0]["status"] == "awaiting_review"
    store.recover()
    assert store.claim() is None
    rid = review.pop("id")
    decision = Decision(decision=choice, decision_key="click", **review)
    args = (store, scheduler.runtime.repo.search, run["session"], rid)
    with pytest.raises(ValueError):
        decide(*args, decision.model_copy(update={"evidence_version": "wrong"}))
    assert decide(*args, decision) == decide(*args, decision)
    resumed = store.claim()
    assert resumed["id"] == run["id"]
    asyncio.run(scheduler.execute(resumed))
    assert calls == (
        ["preview", "other", "final"] if choice == "accept" else ["preview", "other"]
    )
    assert store.claim() is None
    rows = store.query(
        "SELECT verdict FROM task_criterion_assessments WHERE criterion_id='layout' ORDER BY rowid"
    )
    assert [r["verdict"] for r in rows] == [
        "pending",
        "met" if choice == "accept" else "unmet",
    ]


def test_cancel_deactivates_pending_review(tmp_path):
    store, scheduler, run, review, _ = review_run(tmp_path)
    scheduler.cancel(run["id"])
    rid = review.pop("id")
    with pytest.raises(ValueError):
        decide(
            store,
            scheduler.runtime.repo.search,
            run["session"],
            rid,
            Decision(decision="accept", decision_key="late", **review),
        )
    assert store.claim() is None


def test_final_review_http_and_evidence(tmp_path):
    from fastapi.testclient import TestClient
    from artorias_server.api.app import create_http_app

    store, scheduler, run, _, _ = review_run(tmp_path)
    sid = run["session"]
    app = create_http_app(tmp_path, store, scheduler)
    with TestClient(app) as client:
        headers = {"x-artorias-token": client.get("/bootstrap").json()["token"]}
        inbox = client.get(f"/api/sessions/{sid}/reviews", headers=headers).json()
        request = next(r for r in inbox["items"] if r["preview"] == "other")
        url = f"/api/sessions/{sid}/reviews/{request['id']}"
        assert client.get(url + "/evidence", headers=headers).text == "other"
        decision = dict(
            decision="accept",
            decision_key="unique",
            criterion_version=request["criterion_version"],
            evidence_version=request["evidence_version"],
        )
        assert client.post(
            url + "/decision", headers=headers, json=decision
        ).json() == {"state": "accepted"}
        assert client.post(
            url + "/decision", headers=headers, json=decision
        ).json() == {"state": "accepted"}
        assert (
            client.post(
                url + "/decision",
                headers=headers,
                json=dict(decision, decision="request_changes"),
            ).status_code
            == 409
        )
        assert (
            store.query("SELECT status FROM business_tasks WHERE goal='其他工作'")[0][
                "status"
            ]
            == "done"
        )


@pytest.mark.parametrize(
    "claim,expected",
    [("met", "needs_review"), ("partial", "open"), ("unmet", "open")],
)
def test_self_assessment_updates_task(tmp_path, claim, expected):
    from artorias_server.tasks.verification import assess
    from artorias_server.tasks.models import Criterion, FinishStep

    store = Store(tmp_path / "app.sqlite")
    sid = store.session()
    store.submit(sid, "分析", "once")
    run = store.claim()
    runtime = TaskRuntime(tmp_path, store, engine=object())
    item = plan_item("a", "分析")
    item.initial_requirements.criteria = [Criterion(text="完成分析")]
    runtime.repo.install(run, Plan(kind="tasks", items=[item]), [])
    step = store.query("SELECT * FROM task_steps")[0]
    result = FinishStep(step_outcome="completed", final_text="结论及原因",
                        criterion_claims=[Claim(id="C1", claim=claim, reason="根据本步正文自评")])
    evidence, verdicts = assess(store, run, step, result)
    assert runtime.repo.deliver(run, step, result, evidence, verdicts) == expected
    assert list(evidence) == ["response"]
    assert not store.query("SELECT id FROM task_review_requests")
    progress = json.loads(store.query("SELECT progress FROM business_tasks")[0]["progress"])
    head = progress["assessment_heads"]["C1"]
    assert head["claim"] == claim
    assert head["verdict"] == "pending"
    assert head["method"] == "self" and head["reason"] == "根据本步正文自评"
    summary = store.query("SELECT summary FROM business_tasks")[0]["summary"]
    assert f"完成分析 自评 {claim}" in summary


def test_planner_rejects_program_verification():
    from pydantic import ValidationError
    from artorias_server.tasks.models import Criterion

    with pytest.raises(ValidationError):
        Criterion(text="发布文件", verification={"method": "program", "rule": "published"})
