"""Resolve evidence from this execution only. User-facing claims are proposals."""

import hashlib
import json
from pathlib import Path

from .memory import digest


def resolve(store, run, step, result):
    evidence = {
        "response": {
            "artifact_id": "response",
            "version": digest(result.final_text),
            "role": "response",
        }
    }
    if result.deliveries:
        tables = {
            t["name"]
            for t in store.query("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "resource_changes" in tables:
            for delivery in result.deliveries:
                changes = store.query(
                    """SELECT o.path,o.after_hash,r.blob_root FROM workspace_operations o
                    JOIN resource_changes r ON r.change_id=o.id WHERE o.id=? AND o.run=? AND o.session=?
                    AND r.task_id=? AND o.status='committed' AND o.after_hash IS NOT NULL""",
                    (delivery.artifact_id, run["id"], run["session"], step["task"]),
                )
                if changes:
                    change = changes[0]
                    blob = Path(change["blob_root"]) / change["after_hash"]
                    if (
                        hashlib.sha256(blob.read_bytes()).hexdigest()
                        != change["after_hash"]
                    ):
                        raise ValueError("Workspace blob changed")
                    evidence[delivery.artifact_id] = dict(
                        artifact_id=delivery.artifact_id,
                        role=delivery.role,
                        version=change["after_hash"],
                        path=change["path"],
                        change_id=delivery.artifact_id,
                    )
        rows = (
            store.query(
                """SELECT id,result FROM python_executions WHERE session_id=? AND run_id=?
            AND task_id=? AND task_step_id=? AND status='completed' """,
                (run["session"], run["id"], step["task"], step["key"]),
            )
            if "python_executions" in tables
            else []
        )
        for row in rows:
            execution = json.loads(row["result"])
            published = {p["artifact_id"]: p for p in execution.get("published", [])}
            for item in execution.get("artifacts", []):
                aid = item["id"]
                if aid not in published or item["action"] != "write":
                    continue
                for delivery in result.deliveries:
                    if delivery.artifact_id == aid:
                        path = (
                            Path(store.path).parent
                            / "executions"
                            / row["id"]
                            / "artifacts"
                            / aid
                        )
                        if (
                            hashlib.sha256(path.read_bytes()).hexdigest()
                            != item["sha256"]
                        ):
                            raise ValueError("Artifact content changed")
                        evidence[aid] = dict(
                            artifact_id=aid,
                            version=item["sha256"],
                            role=delivery.role,
                            path=item["path"],
                            execution_id=row["id"],
                            change_id=published[aid]["change_id"],
                        )
    roles = [x.role for x in result.deliveries]
    if (
        len(set(roles)) != len(roles)
        or "response" in roles
        or len(evidence) != 1 + len(roles)
    ):
        raise ValueError("Invalid, duplicate or unpublished delivery")
    spec = json.loads(step["scope"])["step_spec"]
    rules = json.loads(step["effective_requirements"])["criteria"]
    local = spec["local_reviews"]
    for entry in [
        *result.milestones,
        *result.blocker_changes,
    ]:
        if set(entry.evidence_refs) - evidence.keys():
            raise ValueError("Unknown evidence reference")
    if len({c.id for c in result.criterion_claims}) != len(result.criterion_claims):
        raise ValueError("Duplicate claim")
    for claim in result.criterion_claims:
        rule = local.get(claim.id) or rules.get(claim.id)
        if not rule or (claim.id not in local and spec["criterion_refs"] and claim.id not in spec["criterion_refs"]):
            raise ValueError("Criterion outside this step")
        if rule["verification"]["method"] == "user" and claim.artifact_id and claim.artifact_id not in evidence:
            raise ValueError("Criterion refers to an undeclared delivery")
    return evidence, rules, local


def assess(store, run, step, result):
    evidence, rules, local = resolve(store, run, step, result)
    assessments = []
    for claim in result.criterion_claims:
        rule = local.get(claim.id) or rules[claim.id]
        policy = rule["verification"]
        user_review = policy["method"] == "user"
        assessment = dict(
            id=claim.id,
            v=rule.get("v", 1),
            local=claim.id in local,
            method="user" if user_review else "self",
            evidence_ref=claim.artifact_id if user_review and claim.artifact_id else "response",
            verdict="pending",
            reason=claim.reason,
            claim=claim.claim,
        )
        if user_review and claim.claim == "met":
            assessment["question"] = policy["question"]
        assessments.append(assessment)
    return evidence, assessments
