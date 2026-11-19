import json
from artorias_shared.ids import uid
from .matching import terms
from .requirements import requirements_for, initial_requirements, apply_patch
from .models import Plan
from artorias_server.storage.migrations import ensure_task_fields

class TaskRepository:
    def __init__(self, store):
        self.store = store
        from artorias_server.workspace.catalog import WorkspaceCatalog
        self.workspaces = WorkspaceCatalog(store)
        with store.tx() as c:
            c.executescript('''
CREATE TABLE IF NOT EXISTS business_tasks(id TEXT PRIMARY KEY, session TEXT NOT NULL, source_task TEXT, goal TEXT NOT NULL, constraints TEXT NOT NULL, status TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '', updated INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS task_session ON business_tasks(session,updated);
CREATE TABLE IF NOT EXISTS task_plans(run TEXT PRIMARY KEY, plan TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS task_steps(run TEXT, key TEXT, task TEXT, scope TEXT NOT NULL, status TEXT NOT NULL, result TEXT NOT NULL DEFAULT '', PRIMARY KEY(run,key));
CREATE TABLE IF NOT EXISTS context_snapshots(run TEXT, key TEXT, body TEXT NOT NULL, PRIMARY KEY(run,key));
''')
            ensure_task_fields(c)
    def candidates(self, session, text):
        recent = self.store.query('SELECT * FROM business_tasks WHERE session=? ORDER BY updated_at DESC,rowid DESC LIMIT 200', (session,))
        # Explicit same-session IDs bypass the recent window, without granting cross-session access.
        import re
        explicit = []
        for tid in set(re.findall(r'\b[a-f0-9]{32}\b', text)):
            explicit.extend(self.store.query('SELECT * FROM business_tasks WHERE session=? AND id=?', (session, tid)))
        query = terms(text)
        ranked = sorted(recent, key=lambda r: len(query & terms(r['goal']+' '+r['summary'])), reverse=True)
        selected = explicit + recent[:8] + [r for r in ranked if query & terms(r['goal']+' '+r['summary'])][:8]
        result = []
        seen = set()
        for row in selected:
            if row['id'] in seen: continue
            seen.add(row['id'])
            candidate={k:row[k] for k in ('id','goal','status','revision','source_task','updated_at')}
            candidate['summary']=row['summary'][-2000:]
            candidate['effective_requirements']=requirements_for(row)
            result.append(candidate)
        return result[:20]
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
            ready = [i for i in remaining if set(i.depends_on)<=set(x.key for x in ordered)]
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
            source = c.execute("SELECT id FROM messages WHERE run=? AND role='user' ORDER BY rowid LIMIT 1", (run['id'],)).fetchone()
            if not source: raise ValueError('Missing source message')
            c.execute('INSERT INTO task_plans(run,plan) VALUES(?,?)',(run['id'],plan.model_dump_json()))
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
                if patch.operations and patch.expected_revision != revision:
                    raise ValueError('Task patch revision conflict')
                effective = apply_patch(baseline, patch, run['input'])
                permanent = effective if patch.scope == 'persistent' else baseline
                applied = revision + int(bool(patch.operations) and patch.scope == 'persistent')
                if reused:
                    if applied != revision:
                        changed = c.execute("UPDATE business_tasks SET status='open',goal=?,constraints=?,requirements=?,revision=? WHERE id=? AND revision=?",
                            (permanent['goal'],json.dumps(list(permanent['constraints'].values()),ensure_ascii=False),json.dumps(permanent,ensure_ascii=False),applied,tid,revision))
                        if changed.rowcount != 1: raise ValueError('Task revision conflict')
                else:
                    c.execute("INSERT INTO business_tasks(id,session,source_task,goal,constraints,status,requirements,revision) VALUES(?,?,?,?,?,'open',?,?)",
                        (tid,run['session'],item.task_id,permanent['goal'],json.dumps(list(permanent['constraints'].values()),ensure_ascii=False),json.dumps(permanent,ensure_ascii=False),applied))
                    c.execute('INSERT INTO task_workspaces(task,workspace_id) VALUES(?,?)',(tid,default['id']))
                c.execute("INSERT INTO task_steps(run,key,task,scope,status,result,source_message_id,base_revision,applied_revision,task_patch,effective_requirements,baseline_requirements) VALUES(?,?,?,?,'pending','',?,?,?,?,?,?)",
                    (run['id'],item.key,tid,item.model_dump_json(),source[0],revision,applied,patch.model_dump_json(),json.dumps(effective,ensure_ascii=False),json.dumps(baseline,ensure_ascii=False)))
        return plan

    def deliver(self, run, step, final):
        with self.store.tx() as c:
            current = c.execute('SELECT status FROM task_steps WHERE run=? AND key=?', (run['id'], step['key'])).fetchone()
            if current['status'] == 'delivered': return
            row = c.execute('SELECT * FROM business_tasks WHERE id=?', (step['task'],)).fetchone()
            progress = json.loads(row['progress'])
            record = {'run_id': run['id'], 'step_key': step['key'], 'source_message_id': step['source_message_id'],
                      'scope_goal': json.loads(step['scope'])['goal'], 'status': 'delivered',
                      'excerpt': final[:1000], 'result_ref': {'run': run['id'], 'key': step['key']}}
            progress.append(record)
            # A bounded materialized ledger, not unverified claims that criteria passed.
            progress = progress[-20:]
            summary = '\n'.join(p['scope_goal'] + ': ' + p['excerpt'][:250] for p in progress)[-6000:]
            c.execute("UPDATE task_steps SET status='delivered',result=? WHERE run=? AND key=?",(final,run['id'],step['key']))
            status = 'needs_review' if row['revision'] == step['applied_revision'] else row['status']
            c.execute('UPDATE business_tasks SET status=?,summary=?,progress=?,updated=updated+1,execution_count=execution_count+1 WHERE id=?',
                (status,summary,json.dumps(progress,ensure_ascii=False),step['task']))
            c.execute('DELETE FROM decisions WHERE run=?',(run['id'],))
