"""Durable execution units. A run job is the dispatcher; the step owns the plan."""

import json
import os

from artorias_shared.ids import uid


def initial_budget():
    return max(1, int(os.getenv("ARTORIAS_WORK_MODEL_CALLS", "80")))


def add_unit(db, plan, kind, inputs, dependencies=()):
    ordinal = db.execute(
        "SELECT COALESCE(MAX(ordinal),0)+1 FROM work_units WHERE plan=?", (plan,)
    ).fetchone()[0]
    unit = uid()
    db.execute(
        "INSERT INTO work_units(id,plan,ordinal,kind,inputs,dependencies,status) VALUES(?,?,?,?,?,?,'pending')",
        (unit, plan, ordinal, kind, json.dumps(inputs), json.dumps(dependencies)),
    )
    return unit


def event(db, plan, kind, **fields):
    payload = dict(plan_id=plan["id"], step_key=plan["step_key"], task_id=plan["task_id"], **fields)
    db.execute(
        "INSERT INTO events(session,run,kind,payload) VALUES(?,?,?,?)",
        (plan["session"], plan["run"], kind, json.dumps(payload, ensure_ascii=False)),
    )


def claim(store, run, workspace, initial):
    with store.tx() as db:
        plan = db.execute(
            "SELECT * FROM work_plans WHERE run=? AND step_key=?", (run["id"], run["step_key"])
        ).fetchone()
        if plan is None:
            revision = db.execute(
                "SELECT applied_revision FROM task_steps WHERE run=? AND key=?",
                (run["id"], run["step_key"]),
            ).fetchone()[0]
            db.execute(
                "INSERT OR IGNORE INTO work_task_budgets(task_id,call_limit) VALUES(?,?)",
                (run["task_id"], max(1, int(os.getenv("ARTORIAS_WORK_TASK_MODEL_CALLS", "300")))),
            )
            key = uid()
            db.execute(
                """INSERT INTO work_plans(id,run,step_key,task_id,session,workspace_id,revision,status,call_limit)
                VALUES(?,?,?,?,?,?,?,'running',?)""",
                (
                    key,
                    run["id"],
                    run["step_key"],
                    run["task_id"],
                    run["session"],
                    workspace,
                    revision,
                    initial_budget(),
                ),
            )
            plan = db.execute("SELECT * FROM work_plans WHERE id=?", (key,)).fetchone()
            for kind, inputs in initial:
                add_unit(db, key, kind, inputs)
            event(db, plan, "work.plan.created")
        if plan["workspace_id"] != workspace or plan["status"] != "running":
            raise ValueError("工作计划状态或工作空间已变化")
        unit = db.execute(
            """SELECT u.* FROM work_units u WHERE u.plan=? AND u.status NOT IN ('completed','obsolete')
                AND NOT EXISTS (
                    SELECT 1 FROM json_each(u.dependencies) d
                    LEFT JOIN work_units parent ON parent.id=d.value AND parent.plan=u.plan
                    WHERE parent.status IS NULL OR parent.status!='completed'
                )
                ORDER BY u.ordinal LIMIT 1""",
            (plan["id"],),
        ).fetchone()
        if unit is None:
            raise ValueError("工作计划没有依赖已就绪的单元")
        active = db.execute(
            "SELECT id FROM work_attempts WHERE unit=? AND status='running'", (unit["id"],)
        ).fetchone()
        if active and run.get("decision"):
            attempt = active[0]
        else:
            if active:
                db.execute("UPDATE work_attempts SET status='interrupted' WHERE id=?", (active[0],))
            attempt = uid()
            db.execute(
                "INSERT INTO work_attempts(id,unit,status) VALUES(?,?,'running')", (attempt, unit["id"])
            )
            db.execute("UPDATE work_units SET status='running' WHERE id=?", (unit["id"],))
            event(
                db,
                plan,
                "work.unit.started",
                unit_id=unit["id"],
                attempt_id=attempt,
                kind_name=unit["kind"],
            )
        return dict(plan), dict(unit), attempt


def consume(db, plan, attempt):
    row = db.execute(
        """UPDATE work_plans SET calls=calls+1 WHERE id=? AND status='running' AND calls<call_limit
        AND EXISTS(SELECT 1 FROM work_task_budgets b WHERE b.task_id=work_plans.task_id AND b.calls<b.call_limit)
        AND EXISTS(SELECT 1 FROM work_attempts WHERE id=? AND status='running' AND started_at>=unixepoch()-3600)
        RETURNING calls""",
        (plan, attempt),
    ).fetchone()
    if row:
        db.execute(
            "UPDATE work_task_budgets SET calls=calls+1 WHERE task_id=(SELECT task_id FROM work_plans WHERE id=?)",
            (plan,),
        )
        db.execute("UPDATE work_attempts SET calls=calls+1 WHERE id=?", (attempt,))
    return bool(row)


def complete_unit(db, plan, unit, attempt, summary, evidence):
    if not db.execute(
        "UPDATE work_attempts SET status='completed' WHERE id=? AND status='running'", (attempt,)
    ).rowcount:
        raise ValueError("执行尝试已失效")
    db.execute(
        "UPDATE work_units SET status='completed',summary=?,evidence=? WHERE id=?",
        (summary, evidence, unit["id"]),
    )
    event(
        db, plan, "work.unit.completed", unit_id=unit["id"], attempt_id=attempt, kind_name=unit["kind"]
    )


def suspend_attempt(db, plan, reason):
    rows = db.execute(
        """UPDATE work_attempts SET status='suspended' WHERE status='running'
        AND unit IN (SELECT id FROM work_units WHERE plan=?) RETURNING unit,id""",
        (plan["id"],),
    ).fetchall()
    for unit, attempt in rows:
        db.execute("UPDATE work_units SET status='suspended' WHERE id=?", (unit,))
        event(db, plan, "work.unit.suspended", unit_id=unit, attempt_id=attempt, reason=reason)


def park(store, run, payload):
    with store.tx() as db:
        plan = db.execute(
            "SELECT * FROM work_plans WHERE run=? AND id=?", (run["id"], payload["plan_id"])
        ).fetchone()
        if plan["status"] != "running":
            return
        db.execute(
            "UPDATE work_plans SET status='waiting',reason=?,generation=generation+1 WHERE id=?",
            (payload["reason"], plan["id"]),
        )
        suspend_attempt(db, plan, payload["reason"])
        store.requeue_step(db, run["id"], plan["step_key"])
        task = db.execute(
            "SELECT calls,call_limit FROM work_task_budgets WHERE task_id=?", (plan["task_id"],)
        ).fetchone()
        progress = db.execute(
            """SELECT count(*) total,sum(status='completed') done FROM work_units
            WHERE plan=? AND status!='obsolete'""",
            (plan["id"],),
        ).fetchone()
        event(
            db,
            plan,
            "work.plan.waiting",
            units_completed=progress["done"],
            units_total=progress["total"],
            task_calls=task["calls"],
            task_limit=task["call_limit"],
            reason=payload["reason"],
            generation=plan["generation"] + 1,
            calls=plan["calls"],
            call_limit=plan["call_limit"],
        )


def resume(db, session, text, key, workspace, plan_id=None, generation=None, extra=0):
    old = db.execute(
        "SELECT * FROM work_commands WHERE session=? AND request_key=?", (session, key)
    ).fetchone()
    if old:
        if old["input"] != text or (plan_id and plan_id != old["plan"]):
            raise ValueError("幂等键对应其他操作")
        return dict(
            db.execute(
                "SELECT r.* FROM runs r JOIN work_plans p ON p.run=r.id WHERE p.id=?", (old["plan"],)
            ).fetchone()
        )
    if (
        plan_id
        and db.execute(
            "SELECT 1 FROM runs WHERE session=? AND request_key=?", (session, key)
        ).fetchone()
    ):
        raise ValueError("幂等键对应其他消息")
    if not plan_id and text.strip() not in ("继续", "继续执行", "继续任务"):
        return
    plans = db.execute(
        """SELECT p.* FROM work_plans p JOIN runs r ON r.id=p.run
        WHERE p.session=? AND p.status='waiting' AND r.status IN ('work_waiting','queued','running','waiting','awaiting_review','analysis_waiting') AND (? IS NULL OR p.id=?)""",
        (session, plan_id, plan_id),
    ).fetchall()
    if not plans and not plan_id:
        return
    if len(plans) != 1:
        raise ValueError("请选择要继续的工作计划" if plans else "工作计划已恢复或结束")
    plan = plans[0]
    if (
        db.execute("SELECT 1 FROM decisions WHERE run=?", (plan["run"],)).fetchone()
        or db.execute(
            "SELECT 1 FROM runs WHERE id=? AND status IN ('waiting','analysis_waiting')", (plan["run"],)
        ).fetchone()
    ):
        raise ValueError("当前 run 正在处理其他审批或设备请求，请在其完成后继续")
    binding = db.execute(
        "SELECT workspace_id FROM run_workspace_bindings WHERE run=? AND task=''", (plan["run"],)
    ).fetchone()
    task_revision = db.execute(
        "SELECT revision FROM business_tasks WHERE id=?", (plan["task_id"],)
    ).fetchone()
    if (
        not binding
        or binding[0] != workspace
        or not task_revision
        or task_revision[0] != plan["revision"]
    ):
        raise ValueError("工作空间或任务需求已变化，请重新规划")
    if generation is not None and plan["generation"] != generation:
        raise ValueError("工作计划版本已变化")
    task = db.execute("SELECT * FROM work_task_budgets WHERE task_id=?", (plan["task_id"],)).fetchone()
    if (plan["calls"] >= plan["call_limit"] or task["calls"] >= task["call_limit"]) and extra == 0:
        raise ValueError("模型调用预算已用完，请选择追加预算并继续")
    if task["calls"] >= task["call_limit"]:
        db.execute(
            "UPDATE work_task_budgets SET call_limit=call_limit+? WHERE task_id=?",
            (extra, plan["task_id"]),
        )
    db.execute(
        "UPDATE work_plans SET status='running',reason='',call_limit=call_limit+? WHERE id=?",
        (extra, plan["id"]),
    )
    if db.execute(
        "UPDATE runs SET status='queued' WHERE id=? AND status IN ('work_waiting','awaiting_review')",
        (plan["run"],),
    ).rowcount:
        db.execute("UPDATE jobs SET status='queued' WHERE run=?", (plan["run"],))
    db.execute(
        "INSERT INTO work_commands(session,request_key,plan,input) VALUES(?,?,?,?)",
        (session, key, plan["id"], text),
    )
    db.execute(
        "INSERT INTO messages(id,session,run,role,content) VALUES(?,?,?,'user',?)",
        (uid(), session, plan["run"], text),
    )
    event(db, plan, "work.plan.resumed", text=text, added_calls=extra, control_turn=uid())
    return dict(db.execute("SELECT * FROM runs WHERE id=?", (plan["run"],)).fetchone())
