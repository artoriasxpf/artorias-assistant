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
    delivery_refs = set(evidence)
    spec = json.loads(step["scope"])["step_spec"]
    rules = json.loads(step["effective_requirements"])["criteria"]
    local = spec["local_reviews"]
    references = {
        ref
        for entry in [*result.milestones, *result.blocker_changes]
        for ref in entry.evidence_refs
    }
    missing_refs = references - evidence.keys()
    if missing_refs and store.query(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='python_executions'"
    ):
        # An execution receipt can support a milestone or blocker update even
        # when the script produced no file to declare as a delivery.
        placeholders = ",".join("?" for _ in missing_refs)
        receipts = store.query(
            f"""SELECT id,result FROM python_executions WHERE session_id=? AND run_id=?
                AND task_id=? AND task_step_id=? AND id IN ({placeholders})""",
            (run["session"], run["id"], step["task"], step["key"], *sorted(missing_refs)),
        )
        for row in receipts:
            receipt = json.loads(row["result"]) if row["result"] else {}
            status = receipt.get("execution_status")
            if status in ("succeeded", "failed", "timed_out"):
                evidence[row["id"]] = {
                    "role": "execution",
                    "execution_id": row["id"],
                    "execution_status": status,
                    "exit_code": receipt.get("exit_code"),
                    "version": digest(row["result"]),
                }
    unknown = references - evidence.keys()
    if unknown:
        raise ValueError(
            "无法验证本步骤的 evidence_refs：" + ", ".join(sorted(unknown)[:5])
            + "。可使用 response、本次已声明的交付 ID 或本步骤已结束的执行 ID。"
        )
    claim_ids = {claim.id for claim in result.criterion_claims}
    if len(claim_ids) != len(result.criterion_claims):
        raise ValueError("Duplicate claim")
    required_claims = set(spec["criterion_refs"]) or {
        key for key, rule in rules.items() if rule["required"]
    }
    required_claims.update(key for key, rule in local.items() if rule["required"])
    missing = required_claims - claim_ids
    if missing:
        raise ValueError("criterion_claims 缺少标准：" + ", ".join(sorted(missing)))
    for claim in result.criterion_claims:
        rule = local.get(claim.id) or rules.get(claim.id)
        if not rule or (claim.id not in local and spec["criterion_refs"] and claim.id not in spec["criterion_refs"]):
            raise ValueError("Criterion outside this step")
        if rule["verification"]["method"] == "user" and claim.artifact_id and claim.artifact_id not in delivery_refs:
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
