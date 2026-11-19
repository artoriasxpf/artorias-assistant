"""Durable local execution plans/results. Unknown execution is never blindly replayed."""
import json
from .migrations import ensure_timestamps

class ExecutionRepository:
    def __init__(self, store):
        self.store = store
        with store.tx() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS python_executions(
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, run_id TEXT,
                task_id TEXT, task_step_id TEXT, tool_call_id TEXT NOT NULL,
                request_hash TEXT NOT NULL, plan TEXT NOT NULL, status TEXT NOT NULL,
                result TEXT, UNIQUE(session_id,run_id,task_id,tool_call_id))''')
            c.execute('''CREATE TABLE IF NOT EXISTS execution_publications(
                operation_id TEXT PRIMARY KEY, execution_id TEXT NOT NULL,
                target TEXT NOT NULL, status TEXT NOT NULL, result TEXT)''')
            ensure_timestamps(c)

    def get(self, execution_id):
        rows = self.store.query('SELECT * FROM python_executions WHERE id=?',(execution_id,))
        if not rows:return None
        row=rows[0];row['plan']=json.loads(row['plan'])
        row['result']=json.loads(row['result']) if row['result'] else None
        return row

    def install(self, plan):
        ctx=plan['context']
        with self.store.tx() as c:
            c.execute('INSERT OR IGNORE INTO python_executions(id,session_id,run_id,task_id,task_step_id,tool_call_id,request_hash,plan,status) VALUES(?,?,?,?,?,?,?,?,?)',
                (plan['id'],ctx['session'],ctx['run'],ctx['task'],ctx.get('step'),ctx['call'],plan['request_hash'],json.dumps(plan), 'awaiting_approval'))
        return self.get(plan['id'])

    def transition(self, eid, before, after):
        with self.store.tx() as c:
            return bool(c.execute('UPDATE python_executions SET status=? WHERE id=? AND status=?',(after,eid,before)).rowcount)

    def save(self,eid,status,result):
        with self.store.tx() as c:
            c.execute('UPDATE python_executions SET status=?,result=? WHERE id=?',(status,json.dumps(result),eid))

    def publication(self, op):
        rows=self.store.query('SELECT * FROM execution_publications WHERE operation_id=?',(op,))
        return rows[0] if rows else None

    def published(self, eid, op, target, result):
        with self.store.tx() as c:
            c.execute('INSERT INTO execution_publications(operation_id,execution_id,target,status,result) VALUES(?,?,?,?,?) ON CONFLICT(operation_id) DO UPDATE SET status=excluded.status,result=excluded.result',(op,eid,target,'completed',json.dumps(result)))
