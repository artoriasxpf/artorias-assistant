import json
import logging
from artorias_server.integrations.search.models import IndexUnavailable
from artorias_shared.ids import uid
from artorias_server.integrations.search.tasks.indexing import TaskSearch
from .requirements import requirements_for, initial_requirements, apply_patch
from .models import Plan
from pathlib import Path
from .memory import refresh, deliver_result

class TaskRepository:
    def __init__(self, store, *, text_index=None):
        self.store = store
        from artorias_server.workspace.catalog import WorkspaceCatalog
        self.workspaces = WorkspaceCatalog(store)
        with store.tx() as c:
            schema = Path(__file__).parents[1] / 'storage' / 'task_schema.sql'
            c.executescript(schema.read_text())
        self.search = TaskSearch(store, text_index)
        try:
            self.search.prepare()
        except IndexUnavailable as exc:
            logging.getLogger(__name__).warning('task.index unavailable: %s', exc)

    def rebuild_search(self):
        """Rebuild from authoritative tasks, including after a missing-index repair."""
        return self.search.prepare(force=True)

    def candidates(self, session, text):
        return self.search.candidates(session, text)
    def install(self, run, plan, candidates):
        existing = self.store.query('SELECT plan FROM task_plans WHERE run=?', (run['id'],))
        if existing:
            return Plan.model_validate_json(existing[0]['plan'])
        allowed = {r['id']:r for r in candidates}
        keys = [i.key for i in plan.items]
        if len(keys)!=len(set(keys)) or (plan.kind=='tasks') != bool(keys):
            raise ValueError('Invalid task count/keys')
        ordered, remaining = [], list(plan.items)
        while remaining:
            ready = [i for i in remaining if (set(i.depends_on) | {w.step_key for w in i.step_spec.wait_for})<=set(x.key for x in ordered)]
            if not ready: raise ValueError('Invalid or cyclic dependencies')
            ordered.extend(ready)
            remaining = [i for i in remaining if i not in ready]
        reused = set()
        for item in ordered:
            if item.relation == 'new':
                if item.task_id: raise ValueError('New task cannot reference an existing ID')
            elif item.task_id not in allowed:
                raise ValueError('Task outside candidate/session scope')
            if item.relation in ('continue','refine'):
                if item.task_id in reused: raise ValueError('Duplicate task execution')
                reused.add(item.task_id)
        plan.items = ordered
        default = self.workspaces.snapshot_run(run['id'],run['session'])
        for item in ordered:
            if item.relation in ('continue','refine'):
                self.workspaces.task_binding(item.task_id,run['session'])
        with self.store.tx() as c:
            saved = c.execute('SELECT plan FROM task_plans WHERE run=?',(run['id'],)).fetchone()
            if saved:
                return Plan.model_validate_json(saved[0])
            source = c.execute("SELECT id FROM messages WHERE run=? AND role='user' ORDER BY rowid LIMIT 1", (run['id'],)).fetchone()
            if not source: raise ValueError('Missing source message')
            c.execute('INSERT INTO task_plans(run,plan) VALUES(?,?)',(run['id'],plan.model_dump_json()))
            plan_aliases = {}
            for item in ordered:
                reused = item.relation in ('continue', 'refine')
                tid = item.task_id if reused else uid()
                if reused:
                    row = c.execute('SELECT * FROM business_tasks WHERE id=? AND session=?', (tid, run['session'])).fetchone()
                    if row is None: raise ValueError('Task outside session')
                    task = dict(row)
                    baseline = requirements_for(task)
                    revision = task['revision']
                    # Detect stale planner state even when it proposed no patch.
                    if revision != allowed[tid]['revision']: raise ValueError('Task revision conflict; replan required')
                else:
                    baseline = initial_requirements(item)
                    revision = 0
                patch = item.task_patch
                if reused and patch.expected_revision != revision:
                    raise ValueError('Task patch revision conflict')
                effective, aliases = apply_patch(baseline, patch, run['input'])
                plan_aliases[item.key] = aliases
                permanent = effective
                spec = item.step_spec
                for wait in spec.wait_for:
                    wait.criterion_ref = plan_aliases[wait.step_key].get(wait.criterion_ref, wait.criterion_ref)
                spec.criterion_refs = [aliases.get(k,k) for k in spec.criterion_refs]
                if set(spec.criterion_refs) - effective['criteria'].keys() or spec.local_reviews.keys() & effective['criteria'].keys():
                    raise ValueError('Unknown or ambiguous criterion reference')
                goal, constraints, criteria = (
                    permanent['goal'], json.dumps(permanent['constraints'],ensure_ascii=False),
                    json.dumps(permanent['criteria'],ensure_ascii=False),
                )
                applied = revision + int(effective != baseline)
                if reused:
                    if applied != revision:
                        changed = c.execute("UPDATE business_tasks SET status='open',goal=?,constraints=?,criteria=?,revision=? WHERE id=? AND revision=?",
                            (goal,constraints,criteria,applied,tid,revision))
                        if changed.rowcount != 1: raise ValueError('Task revision conflict')
                else:
                    c.execute("INSERT INTO business_tasks(id,session,source_task,goal,constraints,criteria,status,revision) VALUES(?,?,?,?,?,?,'open',?)",
                        (tid,run['session'],item.task_id,goal,constraints,criteria,applied))
                    c.execute('INSERT INTO task_workspaces(task,workspace_id) VALUES(?,?)',(tid,default['id']))
                if not reused or applied != revision:
                    from .reviews import supersede
                    supersede(c, tid, effective["criteria"])
                    refresh(c, tid, self.search)
                c.execute("INSERT INTO task_steps(run,key,task,scope,status,result,source_message_id,base_revision,applied_revision,task_patch,effective_requirements,baseline_requirements) VALUES(?,?,?,?,'pending','',?,?,?,?,?,?)",
                    (run['id'],item.key,tid,item.model_dump_json(),source[0],revision,applied,patch.model_dump_json(),json.dumps(effective,ensure_ascii=False),json.dumps(baseline,ensure_ascii=False)))
                for wait in spec.wait_for:
                    producer = c.execute('SELECT scope,effective_requirements FROM task_steps WHERE run=? AND key=?',
                                         (run['id'],wait.step_key)).fetchone()
                    rules = json.loads(producer['effective_requirements'])['criteria']
                    local = json.loads(producer['scope'])['step_spec']['local_reviews']
                    if wait.criterion_ref not in rules and wait.criterion_ref not in local:
                        raise ValueError('Unknown dependency criterion')
                    policy = (local.get(wait.criterion_ref) or rules[wait.criterion_ref])["verification"]
                    if policy["method"] != "user":
                        raise ValueError("A gated dependency requires explicit user confirmation")
                    version = rules.get(wait.criterion_ref,{}).get('v',1)
                    c.execute("INSERT INTO task_step_waits(run,consumer,producer,criterion_id,criterion_version,artifact_role,state) VALUES(?,?,?,?,?,?,'pending')",
                              (run['id'],item.key,wait.step_key,wait.criterion_ref,version,wait.artifact_role))
            c.execute('UPDATE task_plans SET plan=? WHERE run=?',(plan.model_dump_json(),run['id']))
        return plan

    def deliver(self, run, step, result, evidence, verdicts, *, analysis_result=None):
        with self.store.tx() as c:
            status = deliver_result(c, self.search, run, step, result, evidence, verdicts)
            if analysis_result:
                c.execute('UPDATE analyses SET status=? WHERE id=? AND revision=?',
                          ('open' if analysis_result['partial'] else 'completed',
                           analysis_result['analysis_id'], analysis_result['revision']))
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='analysis_executions'").fetchone():
                c.execute('DELETE FROM analysis_executions WHERE run_id=? AND step_key=?',
                          (run['id'], step['key']))
            return status
