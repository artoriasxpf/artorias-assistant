"""Version-bound review decisions and durable scheduling on the local queue."""

import json
from typing import Literal

from pydantic import BaseModel, Field
from artorias_shared.ids import uid
from .memory import dump, event, refresh


class Decision(BaseModel):
    decision: Literal["accept", "request_changes"]
    decision_key: str = Field(min_length=1, max_length=100)
    criterion_version: int
    evidence_version: str
    feedback: str = Field(default="", max_length=2000)


def wake(c, run):
    # A parked run is rescanned when any gate changes; the executor checks all gates.
    updated = c.execute(
        "UPDATE runs SET status='queued' WHERE id=? AND status IN ('awaiting_review','work_waiting')",
        (run,),
    )
    if updated.rowcount:
        c.execute("UPDATE jobs SET status='queued' WHERE run=?", (run,))


def supersede(c, task_id, rules, *, criterion=None):
    for edge in c.execute(
        """SELECT w.rowid,w.*,a.criterion_id AS checked FROM task_step_waits w
            JOIN task_criterion_assessments a ON a.id=w.assessment_id
            JOIN task_steps s ON s.run=w.run AND s.key=w.consumer
            WHERE a.task_id=? AND a.local=0 AND w.state='satisfied' AND s.status='pending'""",
        (task_id,),
    ).fetchall():
        if (
            rules.get(edge["checked"], {}).get("v") != edge["criterion_version"]
            or criterion == edge["checked"]
        ):
            c.execute(
                "UPDATE task_step_waits SET state='needs_replan' WHERE rowid=?",
                (edge["rowid"],),
            )
            wake(c, edge["run"])
    rows = c.execute(
        """SELECT r.*,a.criterion_id,a.criterion_version,a.local
        FROM task_review_requests r JOIN task_criterion_assessments a ON a.id=r.assessment_id
        WHERE r.task_id=? AND r.state='pending'""",
        (task_id,),
    ).fetchall()
    for row in rows:
        key = row["criterion_id"]
        if row["local"] or (
            criterion != key and rules.get(key, {}).get("v") == row["criterion_version"]
        ):
            continue
        c.execute(
            "UPDATE task_review_requests SET state='superseded' WHERE id=?",
            (row["id"],),
        )
        c.execute(
            "UPDATE task_step_waits SET state='needs_replan' WHERE assessment_id=?",
            (row["assessment_id"],),
        )
        event(
            c,
            row["session"],
            row["run"],
            "review.closed",
            {"id": row["id"], "state": "superseded"},
        )
        wake(c, row["run"])


def decide(store, search, session, rid, decision):
    with store.tx() as c:
        row = c.execute(
            """SELECT r.*,a.criterion_id,a.criterion_version,a.evidence_ref,a.evidence_version,a.local
            FROM task_review_requests r JOIN task_criterion_assessments a ON a.id=r.assessment_id
            WHERE r.id=? AND r.session=?""",
            (rid, session),
        ).fetchone()
        if row is None:
            raise KeyError(rid)
        if (decision.criterion_version, decision.evidence_version) != (
            row["criterion_version"],
            row["evidence_version"],
        ):
            raise ValueError("确认版本已变更")
        accepted = decision.decision == "accept"
        state = "accepted" if accepted else "changes_requested"
        if row["state"] != "pending":
            if (row["state"], row["decision_key"], row["feedback"]) == (
                state,
                decision.decision_key,
                decision.feedback,
            ):
                return {"state": state}
            raise ValueError("该确认已关闭：" + row["state"])
        task = c.execute(
            "SELECT * FROM business_tasks WHERE id=?", (row["task_id"],)
        ).fetchone()
        if (
            not row["local"]
            and json.loads(task["criteria"]).get(row["criterion_id"], {}).get("v")
            != decision.criterion_version
        ):
            raise ValueError("验收标准已变更")
        verdict = "met" if accepted else "unmet"
        aid = uid()
        c.execute(
            """INSERT INTO task_criterion_assessments(id,task_id,run,step_key,criterion_id,criterion_version,
            local,method,evidence_ref,evidence_version,verdict,reason)
            SELECT ?,task_id,run,step_key,criterion_id,criterion_version,local,method,evidence_ref,evidence_version,?,?
            FROM task_criterion_assessments WHERE id=?""",
            (aid, verdict, decision.feedback, row["assessment_id"]),
        )
        c.execute(
            "UPDATE task_review_requests SET state=?,decision_key=?,feedback=? WHERE id=?",
            (state, decision.decision_key, decision.feedback, rid),
        )
        progress = json.loads(task["progress"])
        if not accepted:
            progress["blockers"].append(
                {
                    "id": rid,
                    "text": decision.feedback or row["question"],
                    "step_ref": {"run": row["run"], "key": row["step_key"]},
                }
            )
        if not row["local"]:
            progress["assessment_heads"][row["criterion_id"]] = {
                "v": decision.criterion_version,
                "verdict": verdict,
                "assessment_id": aid,
            }
        c.execute(
            "UPDATE business_tasks SET progress=? WHERE id=?",
            (dump(progress), task["id"]),
        )
        changed = c.execute(
            "UPDATE task_step_waits SET assessment_id=?,state=? WHERE assessment_id=? AND state=?",
            (
                aid,
                "satisfied" if accepted else "needs_replan",
                row["assessment_id"],
                "pending",
            ),
        )
        event(
            c,
            session,
            row["run"],
            "review.resolved",
            {"id": rid, "state": state, "feedback": decision.feedback},
        )
        refresh(c, task["id"], search)
        if changed.rowcount:
            wake(c, row["run"])
        return {"state": state}


def park_or_requeue(store, run):
    """Atomically recheck dependencies to cover decisions arriving during parking."""
    with store.tx() as c:
        steps = c.execute(
            "SELECT key,status,scope,result_json FROM task_steps WHERE run=?",
            (run["id"],),
        ).fetchall()
        completed = {
            s["key"]
            for s in steps
            if s["status"] == "delivered"
            and json.loads(s["result_json"])["submitted"]["step_outcome"] == "completed"
        }
        pending_review = False
        ready = False
        for step in steps:
            if step["status"] != "pending":
                continue
            states = {
                r[0]
                for r in c.execute(
                    "SELECT state FROM task_step_waits WHERE run=? AND consumer=?",
                    (run["id"], step["key"]),
                )
            }
            pending_review |= "pending" in states
            if "needs_replan" in states or (
                "pending" not in states
                and set(json.loads(step["scope"])["depends_on"]) <= completed
            ):
                ready = True
        if not ready and not pending_review:
            raise ValueError("没有可执行的后继步骤，依赖需要修订")
        state = "queued" if ready else "awaiting_review"
        c.execute("UPDATE runs SET status=? WHERE id=?", (state, run["id"]))
        c.execute("UPDATE jobs SET status=? WHERE run=?", (state, run["id"]))
        if not ready:
            event(c, run["session"], run["id"], "task.awaiting_review", {})
