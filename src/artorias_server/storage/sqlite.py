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
                if decision:result['decision']=decision[0]
                return result
    def finish(self,run,status,text=None):
        with self.tx() as c:
            c.execute('UPDATE runs SET status=? WHERE id=?',(status,run['id']))
            c.execute("UPDATE jobs SET status='done' WHERE run=?",(run['id'],))
            if text is not None:c.execute('INSERT INTO messages(id,session,run,role,content) VALUES(?,?,?,?,?)',(uid(),run['session'],run['id'],'assistant',text))
    def recover(self):
        # Never replay an uncertain side effect after a process crash.
        for run in self.query("SELECT * FROM runs WHERE status='running'"):
            self.finish(run,'interrupted')
            self.emit(run['session'],run['id'],'run.interrupted',{'reason':'进程中断；请核对已产生的文件，再发送新消息继续。'})

    def decide(self,rid,value):
        with self.tx() as c:
            row=c.execute('SELECT status FROM runs WHERE id=?',(rid,)).fetchone()
            if not row or row[0]!='waiting':raise ValueError('审批已处理或不存在')
            c.execute('INSERT INTO decisions(run,value) VALUES(?,?) ON CONFLICT(run) DO UPDATE SET value=excluded.value',(rid,value))
            c.execute("UPDATE runs SET status='queued' WHERE id=?",(rid,))
            c.execute("UPDATE jobs SET status='queued' WHERE run=?",(rid,))
