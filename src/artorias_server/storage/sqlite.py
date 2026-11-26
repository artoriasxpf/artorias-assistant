import json, sqlite3
from contextlib import contextmanager

from artorias_shared.ids import uid

class Store:
    def __init__(self, path):
        self.path = str(path)
        self._schema_version = None
        with self.tx() as c:
            c.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS decisions(run TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,session TEXT,run TEXT,role TEXT,content TEXT);
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,session TEXT,turn TEXT,status TEXT,input TEXT,request_key TEXT, UNIQUE(session,request_key));
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,run TEXT,status TEXT);
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,session TEXT,run TEXT,kind TEXT,payload TEXT);
            CREATE INDEX IF NOT EXISTS event_session ON events(session,seq);
            CREATE TABLE IF NOT EXISTS changes(id TEXT PRIMARY KEY,session TEXT,run TEXT,path TEXT,before_hash TEXT,after_hash TEXT,reverts TEXT);
            """)
    @contextmanager
    def tx(self):
        c=sqlite3.connect(self.path,timeout=5);c.row_factory=sqlite3.Row
        try:
            c.execute('BEGIN IMMEDIATE')
            yield c
            version = c.execute('PRAGMA schema_version').fetchone()[0]
            if version != self._schema_version:
                from .migrations import ensure_timestamps
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
    def claim(self):
        with self.tx() as c:
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
        # Never replay an uncertain side effect after a process crash.
        for run in self.query("SELECT * FROM runs WHERE status='running'"):
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
