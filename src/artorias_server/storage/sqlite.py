import json, sqlite3
from contextlib import contextmanager
from pathlib import Path

from artorias_shared.ids import uid

from .migrations import ensure_timestamps, rename_business_tasks

class Store:
    def __init__(self, path):
        self.path = str(path)
        self._schema_version = None
        with self.tx() as c:
            c.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS decisions(run TEXT PRIMARY KEY, value TEXT, created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,session TEXT,run TEXT,role TEXT,content TEXT, created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,session TEXT,turn TEXT,request_key TEXT,input TEXT,status TEXT, created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '', UNIQUE(session,request_key));
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,run TEXT,status TEXT, created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,session TEXT,run TEXT,kind TEXT,payload TEXT, created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '');
            CREATE INDEX IF NOT EXISTS event_session ON events(session,seq);
            CREATE INDEX IF NOT EXISTS event_run_seq ON events(session,run,seq);
            CREATE TABLE IF NOT EXISTS changes(id TEXT PRIMARY KEY,session TEXT,run TEXT,path TEXT,before_hash TEXT,after_hash TEXT,reverts TEXT, created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '');
            """)
        with self.tx() as c:
            c.executescript((Path(__file__).parents[1]/"work"/"schema.sql").read_text())
        with self.tx() as c:
            rename_business_tasks(c)

    @contextmanager
    def tx(self):
        c=sqlite3.connect(self.path,timeout=5);c.row_factory=sqlite3.Row
        try:
            c.execute('BEGIN IMMEDIATE')
            yield c
            version = c.execute('PRAGMA schema_version').fetchone()[0]
            if version != self._schema_version:
                ensure_timestamps(c)
            c.commit()
            self._schema_version = c.execute('PRAGMA schema_version').fetchone()[0]
        except BaseException:
            c.rollback();raise
        finally:c.close()
    def query(self,sql,args=()):
        with self.tx() as c:return [dict(x) for x in c.execute(sql,args)]
    def session(self):
        s=uid()
        with self.tx() as c:c.execute('INSERT INTO sessions(id) VALUES(?)',(s,))
        return s
    def require(self,s):
        if not self.query('SELECT id FROM sessions WHERE id=?',(s,)):raise KeyError(s)
    def submit(self,s,text,key):
        self.require(s)
        from artorias_server.workspace.catalog import WorkspaceCatalog
        workspace=WorkspaceCatalog(self).current(s)
        with self.tx() as c:
            old=c.execute('SELECT * FROM runs WHERE session=? AND request_key=?',(s,key)).fetchone()
            if old:
                if old['input']!=text:raise ValueError('幂等键对应不同输入')
                return dict(old)
            from artorias_server.work.repository import resume
            resumed = resume(c,s,text,key,workspace['id'])
            if resumed:
                return resumed
            run,turn=uid(),uid()
            c.execute('INSERT INTO runs(id,session,turn,status,input,request_key) VALUES(?,?,?,?,?,?)',(run,s,turn,'queued',text,key))
            c.execute('INSERT INTO messages(id,session,run,role,content) VALUES(?,?,?,?,?)',(uid(),s,run,'user',text))
            c.execute('INSERT INTO jobs(id,run,status) VALUES(?,?,?)',(uid(),run,'queued'))
            c.execute('INSERT INTO run_workspace_bindings(run,task,workspace_id) VALUES(?,?,?)',(run,'',workspace['id']))
            c.execute('INSERT INTO events(session,run,kind,payload) VALUES(?,?,?,?)',(s,run,'message.accepted',json.dumps({'text':text})))
        return {'id':run,'turn':turn,'status':'queued'}
    def emit(self,s,run,kind,payload):
        with self.tx() as c:c.execute('INSERT INTO events(session,run,kind,payload) VALUES(?,?,?,?)',(s,run,kind,json.dumps(payload,ensure_ascii=False)))
    def events(self,s,after):return self.query('SELECT * FROM events WHERE session=? AND seq>? ORDER BY seq LIMIT 100',(s,after))
    def begin_analysis(self, run_id, step_key):
        with self.tx() as c:
            c.execute("""INSERT INTO analysis_executions(run_id,step_key,segment,state)
                VALUES(?,?,0,'active') ON CONFLICT(run_id) DO NOTHING""", (run_id, step_key))
            row = c.execute('SELECT step_key,segment FROM analysis_executions WHERE run_id=?',
                            (run_id,)).fetchone()
            if row['step_key'] != step_key:
                raise ValueError('Analysis execution belongs to a different step')
            c.execute("UPDATE analysis_executions SET state='active' WHERE run_id=? AND state!='side_effect'", (run_id,))
            return row['segment']

    def _suspend_analysis(self, c, run_id, dependency_id):
        changed = c.execute("""UPDATE analysis_executions
            SET state='waiting',segment=segment+1,dependency_id=?,ready_at=unixepoch()+CASE WHEN ? IS NULL THEN 0 ELSE min(60, 2 << min(segment,5)) END
            WHERE run_id=? AND state='active' AND segment<32
                AND started_at>=unixepoch()-3600""",
            (dependency_id, dependency_id, run_id))
        if not changed.rowcount:
            return False
        c.execute("UPDATE runs SET status='analysis_waiting' WHERE id=? AND status='running'", (run_id,))
        c.execute("UPDATE jobs SET status='done' WHERE run=?", (run_id,))
        c.execute('DELETE FROM decisions WHERE run=?', (run_id,))
        c.execute("""UPDATE task_steps SET status='interrupted' WHERE run=? AND key=(
            SELECT step_key FROM analysis_executions WHERE run_id=?) AND status!='delivered'""",
            (run_id, run_id))
        return True

    @staticmethod
    def requeue_step(db, run_id, step_key):
        # Release the transient graph segment. Durable unit attempts and receipts
        # remain available for recovery; a new queue position allows other runs in.
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='analysis_executions'").fetchone():
            db.execute('DELETE FROM analysis_executions WHERE run_id=? AND step_key=?',(run_id,step_key))
        db.execute('DELETE FROM decisions WHERE run=?',(run_id,))
        db.execute("UPDATE runs SET status='queued' WHERE id=?",(run_id,))
        db.execute('DELETE FROM jobs WHERE run=?',(run_id,))
        db.execute("INSERT INTO jobs(id,run,status) VALUES(?,?,'queued')",(uid(),run_id))
        db.execute("UPDATE task_steps SET status='interrupted' WHERE run=? AND key=?",(run_id,step_key))

    def suspend_analysis(self, run, dependency_id):
        with self.tx() as c:
            return self._suspend_analysis(c, run['id'], dependency_id)

    def _queue_due_analysis(self, c):
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='analysis_executions'").fetchone():
            return
        ready = c.execute("""UPDATE analysis_executions SET state='queued' WHERE state='waiting'
            AND ready_at<=unixepoch() AND run_id IN (
                SELECT id FROM runs WHERE status='analysis_waiting') RETURNING run_id""").fetchall()
        for row in ready:
            c.execute("UPDATE runs SET status='queued' WHERE id=?", (row[0],))
            c.execute("UPDATE jobs SET status='queued' WHERE run=?", (row[0],))

    def claim(self):
        with self.tx() as c:
            self._queue_due_analysis(c)
            row=c.execute("SELECT r.* FROM runs r JOIN jobs j ON j.run=r.id WHERE j.status='queued' AND r.status='queued' AND NOT EXISTS (SELECT 1 FROM runs blocked WHERE blocked.session=r.session AND blocked.id!=r.id AND blocked.status='waiting') ORDER BY j.rowid LIMIT 1").fetchone()
            if row:
                c.execute("UPDATE jobs SET status='running' WHERE run=?",(row['id'],));c.execute("UPDATE runs SET status='running' WHERE id=?",(row['id'],))
                result=dict(row)
                decision=c.execute('SELECT value FROM decisions WHERE run=?',(row['id'],)).fetchone()
                if decision:
                    value = decision[0]
                    result['decision'] = json.loads(value) if value.startswith('{') else value
                return result
    def finish(self,run,status,text=None,*,approval=None):
        with self.tx() as c:
            c.execute('UPDATE runs SET status=? WHERE id=?',(status,run['id']))
            c.execute("UPDATE jobs SET status='done' WHERE run=?",(run['id'],))
            if status in ('cancelled','failed','interrupted'):
                c.execute("""UPDATE work_attempts SET status=? WHERE status='running' AND unit IN
                    (SELECT u.id FROM work_units u JOIN work_plans p ON u.plan=p.id WHERE p.run=?)""",(status,run['id']))
                c.execute("""UPDATE work_units SET status=? WHERE status!='completed' AND plan IN
                    (SELECT id FROM work_plans WHERE run=?)""",(status,run['id']))
                c.execute("UPDATE work_plans SET status=? WHERE run=? AND status!='completed'",(status,run['id']))
            if status=='cancelled' and c.execute("SELECT 1 FROM sqlite_master WHERE name='task_review_requests'").fetchone():
                for row in c.execute("SELECT id FROM task_review_requests WHERE run=? AND state='pending'",(run['id'],)).fetchall():
                    c.execute("UPDATE task_review_requests SET state='cancelled' WHERE id=?",(row[0],))
                    c.execute("INSERT INTO events(session,run,kind,payload) VALUES(?,?,'review.closed',?)",
                              (run['session'],run['id'],json.dumps({'id':row[0],'state':'cancelled'})))
                c.execute("UPDATE task_step_waits SET state='cancelled' WHERE run=? AND state='pending'",(run['id'],))
            if status != 'waiting' and c.execute(
                "SELECT 1 FROM sqlite_master WHERE name='analysis_executions'"
            ).fetchone():
                c.execute('DELETE FROM analysis_executions WHERE run_id=?', (run['id'],))
            if text is not None:c.execute('INSERT INTO messages(id,session,run,role,content) VALUES(?,?,?,?,?)',(uid(),run['session'],run['id'],'assistant',text))
            if status in ('failed', 'cancelled', 'completed', 'interrupted') and c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='permission_requests'"
            ).fetchone():
                c.execute(
                    """UPDATE permission_requests SET state='cancelled' WHERE run_id=?
                       AND (state='pending' OR (state='approved' AND choice='approve'))""",
                    (run['id'],),
                )
            if approval is not None:
                c.execute("INSERT INTO events(session,run,kind,payload) VALUES(?,?,'approval.requested',?)",
                          (run['session'],run['id'],json.dumps(approval,ensure_ascii=False)))
    def recover(self):
        for run in self.query("SELECT * FROM runs WHERE status='running'"):
            with self.tx() as c:
                analysis_exists = c.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='analysis_executions'"
                ).fetchone()
                if analysis_exists and self._suspend_analysis(c, run['id'], None):
                    # The fresh read-only segment can ask again for interrupted approvals.
                    if c.execute("SELECT 1 FROM sqlite_master WHERE name='permission_requests'").fetchone():
                        c.execute("DELETE FROM permission_requests WHERE run_id=? AND state='pending'", (run['id'],))
                    continue
                has_work = c.execute("SELECT 1 FROM work_plans WHERE run=?", (run['id'],)).fetchone()
                owned = has_work and c.execute("""SELECT 1 FROM work_plans p WHERE p.run=? AND (
                    (p.status='running' AND EXISTS (SELECT 1 FROM work_units u WHERE u.plan=p.id AND u.status='running')) OR NOT EXISTS (
                      SELECT 1 FROM task_steps s WHERE s.run=p.run AND s.status NOT IN ('delivered','needs_replan')))""",
                                  (run['id'],)).fetchone()
                if owned and not c.execute("""SELECT 1 FROM task_steps WHERE run=? AND status='failed'""",(run['id'],)).fetchone():
                    c.execute("UPDATE runs SET status='queued' WHERE id=?", (run['id'],))
                    c.execute("UPDATE jobs SET status='queued' WHERE run=?", (run['id'],))
                    continue
            self.finish(run,'interrupted')
            self.emit(run['session'],run['id'],'run.interrupted',{'reason':'进程中断；请核对已产生的文件，再发送新消息继续。'})

    def decide(self,rid,value,request_seq=None):
        allowed = ('approve', 'remember', 'reject')
        individual = isinstance(value, dict)
        if not individual and value not in allowed:
            raise ValueError('Invalid decision')
        with self.tx() as c:
            row=c.execute('SELECT session,status FROM runs WHERE id=?',(rid,)).fetchone()
            if not row:
                raise ValueError('审批不存在')
            if request_seq is not None:
                previous = c.execute(
                    """SELECT payload FROM events WHERE run=? AND kind='approval.decided'
                       AND json_extract(payload,'$.request_seq')=? ORDER BY seq DESC LIMIT 1""",
                    (rid, request_seq),
                ).fetchone()
                if previous:
                    payload = json.loads(previous['payload'])
                    if payload.get('decisions' if individual else 'decision') == value:
                        return
                    raise ValueError('此审批已提交不同决定')
            if row['status'] != 'waiting':
                raise ValueError('审批已处理或不存在')
            request=c.execute(
                "SELECT seq,payload FROM events WHERE session=? AND run=? AND kind='approval.requested' ORDER BY seq DESC LIMIT 1",
                (row['session'],rid),
            ).fetchone()
            if request_seq is not None and (not request or request['seq']!=request_seq):
                raise ValueError('审批请求已更新，请处理最新请求')
            pending=json.loads(request['payload']) if request else {}
            items = pending.get('requests', [])
            choices = value if individual else {item['request_id']: value for item in items}
            if individual and (not items or set(choices) != {item['request_id'] for item in items}):
                raise ValueError('请处理当前批次的所有审批项')
            if value == 'remember' and len(items) != 1:
                raise ValueError('请选择一项具体权限')
            from artorias_server.permissions import record_decision
            for item in items:
                choice = choices[item['request_id']]
                if choice not in allowed or (choice == 'remember' and not item.get('allow_remember')):
                    raise ValueError('无效的权限决定')
                permission_ids = item.get('permission_request_ids', [])
                if item.get('permission_request_id'):
                    permission_ids = [item['permission_request_id']]
                for permission_id in permission_ids:
                    record_decision(c, permission_id, row['session'], rid, choice)
            resume_value = json.dumps({
                key: 'approve' if choice == 'remember' else choice for key, choice in choices.items()
            }) if individual else ('approve' if value == 'remember' else value)
            decision={
                'decision':None if individual else value,
                **({'decisions': choices} if individual else {}),
                'task_id':pending.get('task_id'),
                'request_seq':request['seq'] if request else None,
                'requests':[
                    {key: item.get(key) for key in ('request_id','key','kind')}
                    for item in pending.get('requests',[]) if isinstance(item,dict)
                ],
            }
            c.execute('INSERT INTO decisions(run,value) VALUES(?,?) ON CONFLICT(run) DO UPDATE SET value=excluded.value',(rid,resume_value))
            c.execute("UPDATE runs SET status='queued' WHERE id=?",(rid,))
            c.execute("UPDATE jobs SET status='queued' WHERE run=?",(rid,))
            c.execute("INSERT INTO events(session,run,kind,payload) VALUES(?,?,'approval.decided',?)",
                      (row['session'],rid,json.dumps(decision,ensure_ascii=False)))
