"""Transactional task projections. Only server assessments change acceptance state."""

import hashlib
import json

from artorias_shared.ids import uid


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def event(c, session, run, kind, payload):
    c.execute(
        "INSERT INTO events(session,run,kind,payload) VALUES(?,?,?,?)",
        (session, run, kind, dump(payload)),
    )


def refresh(c, task_id, search):
    task = c.execute("SELECT * FROM business_tasks WHERE id=?", (task_id,)).fetchone()
    progress = json.loads(task["progress"])
    rules = json.loads(task["criteria"])
    heads = progress["assessment_heads"]
    progress["assessment_heads"] = {
        key: heads[key]
        if key in heads and heads[key]["v"] == rule["v"]
        else {"v": rule["v"], "verdict": "pending", "assessment_id": None}
        for key, rule in rules.items()
    }
    required = [
        progress["assessment_heads"][k]["verdict"]
        for k, v in rules.items()
        if v["required"]
    ]
    status = "open"
    if progress["blockers"]:
        status = "blocked"
    elif c.execute(
        "SELECT 1 FROM task_review_requests WHERE task_id=? AND state='pending'",
        (task_id,),
    ).fetchone():
        status = "needs_review"
    elif required and all(x == "met" for x in required):
        status = "done"
    elif not required and task["execution_count"]:
        status = "needs_review"
    inputs = [task["goal"], json.loads(task["constraints"]), rules, status, progress]
    fingerprint = digest(dump(inputs))
    parts = [task["goal"], status]
    parts.extend("阻塞：" + b["text"] for b in progress["blockers"])
    parts.extend(m["text"] for m in progress["milestones"][-3:])
    parts.extend(
        d["role"] + " " + d["artifact_id"] for d in progress["deliverables"][-3:]
    )
    parts.extend(
        v["text"] + " " + progress["assessment_heads"][k]["verdict"]
        for k, v in rules.items()
    )
    parts.extend(v["text"] for v in inputs[1].values())
    summary = "；".join(parts)[:1000]
    if fingerprint != task["summary_input_hash"]:
        c.execute(
            "UPDATE business_tasks SET status=?,progress=?,summary=?,summary_input_hash=? WHERE id=?",
            (status, dump(progress), summary, fingerprint, task_id),
        )
        index_hash = digest(dump([task["goal"], inputs[1], rules, summary]))
        if index_hash != task["search_input_hash"]:
            search.update(c, task_id)
            c.execute(
                "UPDATE business_tasks SET search_input_hash=? WHERE id=?",
                (index_hash, task_id),
            )
    return status


def replace_by(items, field, value):
    items[:] = [item for item in items if item[field] != value[field]]
    items.append(value)


def deliver_result(c, search, run, step, result, evidence, verdicts):
    current = c.execute(
        "SELECT result_json FROM task_steps WHERE run=? AND key=?",
        (run["id"], step["key"]),
    ).fetchone()
    submitted = result.model_dump(exclude={"final_text"})
    if current[0]:
        previous = json.loads(current[0])
        if previous["submitted"] != submitted or previous["final_hash"] != digest(
            result.final_text
        ):
            raise ValueError("Conflicting step replay")
        return c.execute(
            "SELECT status FROM business_tasks WHERE id=?", (step["task"],)
        ).fetchone()[0]
    task = c.execute(
        "SELECT * FROM business_tasks WHERE id=?", (step["task"],)
    ).fetchone()
    later_delivery = c.execute(
        """SELECT 1 FROM task_steps newer JOIN task_steps current
           ON current.run=? AND current.key=? AND newer.task=current.task
           WHERE newer.rowid>current.rowid AND newer.status='delivered' LIMIT 1""",
        (run["id"], step["key"]),
    ).fetchone()
    applicable = task["revision"] == step["applied_revision"] and later_delivery is None
    progress = json.loads(task["progress"])
    ref = {"run": run["id"], "key": step["key"]}
    if applicable:
        for value in result.milestones:
            replace_by(
                progress["milestones"], "key", dict(value.model_dump(), step_ref=ref)
            )
        progress["milestones"] = progress["milestones"][-30:]
        for value in result.blocker_changes:
            progress["blockers"] = [
                x for x in progress["blockers"] if x["id"] != value.id
            ]
            if value.op == "open":
                progress["blockers"].append(value.model_dump())
        for item in result.deliveries:
            replace_by(
                progress["deliverables"],
                "role",
                dict(evidence[item.artifact_id], role=item.role),
            )
    assessment_ids = []
    for item in verdicts:
        aid = uid()
        key, version = item["id"], item["v"]
        ev = evidence[item["evidence_ref"]]
        c.execute(
            """INSERT INTO task_criterion_assessments
            (id,task_id,run,step_key,criterion_id,criterion_version,local,method,evidence_ref,evidence_version,verdict,reason)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                aid,
                step["task"],
                run["id"],
                step["key"],
                key,
                version,
                item["local"],
                item["method"],
                item["evidence_ref"],
                ev["version"],
                item["verdict"],
                item["reason"],
            ),
        )
        assessment_ids.append(aid)
        if not applicable:
            continue
        if not item["local"]:
            from .reviews import supersede

            supersede(c, step["task"], json.loads(task["criteria"]), criterion=key)
            progress["assessment_heads"][key] = {
                "v": version,
                "verdict": item["verdict"],
                "assessment_id": aid,
            }
        role = ev.get("role", "response")
        state = {"met": "satisfied", "unmet": "needs_replan", "pending": "pending"}[
            item["verdict"]
        ]
        c.execute(
            """UPDATE task_step_waits SET state=?,assessment_id=?,evidence_version=?
                     WHERE run=? AND producer=? AND criterion_id=? AND criterion_version=? AND artifact_role=?""",
            (state, aid, ev["version"], run["id"], step["key"], key, version, role),
        )
        if item.get("question"):
            rid = uid()
            c.execute(
                """INSERT INTO task_review_requests
                (id,session,task_id,run,step_key,assessment_id,question,state) VALUES(?,?,?,?,?,?,?,'pending')""",
                (
                    rid,
                    run["session"],
                    step["task"],
                    run["id"],
                    step["key"],
                    aid,
                    item["question"],
                ),
            )
            event(
                c,
                run["session"],
                run["id"],
                "review.requested",
                {
                    "id": rid,
                    "question": item["question"],
                    "criterion_version": version,
                    "evidence_version": ev["version"],
                    "evidence": ev,
                    "preview": result.final_text[:2000],
                },
            )
    # A completed producer cannot supply another assessment for an unbound gate.
    c.execute(
        """UPDATE task_step_waits SET state='needs_replan' WHERE run=? AND producer=?
        AND state='pending' AND (assessment_id IS NULL OR assessment_id NOT IN
        (SELECT assessment_id FROM task_review_requests WHERE state='pending'))""",
        (run["id"], step["key"]),
    )
    envelope = dict(
        schema_version=2,
        step_ref=ref,
        task_id=step["task"],
        applied_revision=step["applied_revision"],
        submitted=submitted,
        final_hash=digest(result.final_text),
        verified_deliveries=evidence,
        initial_assessment_ids=assessment_ids,
        merge_status="applied" if applicable else "historical_only",
    )
    c.execute(
        "UPDATE task_steps SET status='delivered',result=?,result_json=? WHERE run=? AND key=?",
        (result.final_text, dump(envelope), run["id"], step["key"]),
    )
    c.execute(
        "UPDATE business_tasks SET execution_count=execution_count+1,progress=? WHERE id=?",
        (dump(progress), step["task"]),
    )
    c.execute("DELETE FROM decisions WHERE run=?", (run["id"],))
    return refresh(c, step["task"], search)
