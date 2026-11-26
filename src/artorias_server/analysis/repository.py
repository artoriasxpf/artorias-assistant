"""SQLite business state for analysis. File and model I/O stay outside transactions."""
import hashlib
import json

from artorias_shared.ids import uid

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS analyses(
        id TEXT PRIMARY KEY, session_id TEXT NOT NULL, task_id TEXT NOT NULL,
        origin_run TEXT NOT NULL, origin_step_key TEXT NOT NULL, workspace_id TEXT NOT NULL,
        plan_json TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'open', UNIQUE(origin_run,origin_step_key))""",
    """CREATE TABLE IF NOT EXISTS analysis_step_links(
        run_id TEXT NOT NULL, step_key TEXT NOT NULL, analysis_id TEXT NOT NULL,
        role TEXT NOT NULL, PRIMARY KEY(run_id,step_key))""",
    """CREATE TABLE IF NOT EXISTS analysis_batches(
        id TEXT PRIMARY KEY, analysis_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
        call_id TEXT NOT NULL, plan_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'planned',
        receipt_json TEXT NOT NULL DEFAULT '[]', receipt_digest TEXT, note_digest TEXT,
        UNIQUE(analysis_id,ordinal), UNIQUE(analysis_id,call_id))""",
    """CREATE TABLE IF NOT EXISTS analysis_notes(
        batch_id TEXT PRIMARY KEY, analysis_id TEXT NOT NULL, seq INTEGER NOT NULL,
        body_json TEXT NOT NULL, UNIQUE(analysis_id,seq))""",
    """CREATE TABLE IF NOT EXISTS analysis_sources(
        id INTEGER PRIMARY KEY, analysis_id TEXT NOT NULL, location_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL, path TEXT NOT NULL, version TEXT NOT NULL,
        kind TEXT NOT NULL, total INTEGER, UNIQUE(analysis_id,location_id,workspace_id,path,version,kind))""",
    """CREATE TABLE IF NOT EXISTS analysis_ranges(
        source_id INTEGER NOT NULL, batch_id TEXT NOT NULL, unit TEXT NOT NULL,
        start INTEGER NOT NULL, end INTEGER NOT NULL,
        PRIMARY KEY(source_id,batch_id,unit,start,end))""",
    """CREATE TABLE IF NOT EXISTS analysis_summaries(
        analysis_id TEXT PRIMARY KEY, version INTEGER NOT NULL, body_json TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS analysis_executions(
        run_id TEXT PRIMARY KEY, step_key TEXT NOT NULL, segment INTEGER NOT NULL,
        model_calls INTEGER NOT NULL DEFAULT 0, started_at INTEGER NOT NULL DEFAULT (unixepoch()),
        state TEXT NOT NULL, ready_at INTEGER, dependency_id TEXT)""",
    "CREATE INDEX IF NOT EXISTS analysis_task_scope ON analyses(session_id,task_id,workspace_id)",
)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


class AnalysisRepository:
    def __init__(self, store):
        self.store = store
        with store.tx() as db:
            for sql in SCHEMA:
                db.execute(sql)

    def attach(self, run, plan, workspace_id, step_key):
        plan_json = encode(plan.model_dump())
        with self.store.tx() as db:
            linked = db.execute("""SELECT a.* FROM analyses a JOIN analysis_step_links l ON a.id=l.analysis_id
                WHERE l.run_id=? AND l.step_key=?""", (run['id'], step_key)).fetchone()
            if linked:
                return dict(linked)
            # Reuse unfinished work only under the same Task, workspace and plan.
            prior = db.execute("""SELECT a.* FROM analyses a WHERE session_id=? AND task_id=? AND workspace_id=?
                AND plan_json=? AND status='open' AND NOT EXISTS (
                    SELECT 1 FROM analysis_step_links l JOIN runs r ON r.id=l.run_id
                    WHERE l.analysis_id=a.id AND r.status IN ('queued','running','waiting','analysis_waiting'))
                ORDER BY a.rowid DESC LIMIT 1""",
                (run['session'], run['task_id'], workspace_id, plan_json)).fetchone()
            analysis_id = prior['id'] if prior else uid()
            if not prior:
                db.execute("""INSERT INTO analyses(id,session_id,task_id,origin_run,origin_step_key,workspace_id,plan_json)
                    VALUES(?,?,?,?,?,?,?)""",
                    (analysis_id, run['session'], run['task_id'], run['id'], step_key, workspace_id, plan_json))
            db.execute('''INSERT INTO analysis_step_links(run_id,step_key,analysis_id,role)
                       VALUES(?,?,?,?)''',
                       (run['id'], step_key, analysis_id, 'continue' if prior else 'owner'))
            return dict(db.execute('SELECT * FROM analyses WHERE id=?', (analysis_id,)).fetchone())

    def _commit(self, db, analysis_id, note):
        body = note.model_dump(mode='json')
        if len(encode(body).encode()) > 12 * 1024:
            raise ValueError('分析笔记超过 12 KiB，请缩短摘要或发现')
        checksum = digest(body)
        batch = db.execute('SELECT * FROM analysis_batches WHERE analysis_id=? AND id=?',
                           (analysis_id, note.batch_id)).fetchone()
        if not batch:
            raise ValueError('找不到本次分析的批次')
        if batch['state'] == 'committed':
            if batch['note_digest'] != checksum:
                raise ValueError('该批次已提交其他结论')
            return
        if batch['state'] != 'presented':
            raise ValueError('批次尚未进入模型上下文')
        receipt = json.loads(batch['receipt_json'])
        if any(index < 0 or index >= len(receipt) or receipt[index]['status'] != 'ok'
               for finding in note.findings for index in finding.refs):
            raise ValueError('引用必须指向本批成功展示的结果')
        seq = db.execute('UPDATE analyses SET revision=revision+1 WHERE id=? RETURNING revision',
                         (analysis_id,)).fetchone()[0]
        db.execute('INSERT INTO analysis_notes(batch_id,analysis_id,seq,body_json) VALUES(?,?,?,?)',
                   (note.batch_id, analysis_id, seq, encode({'note': body, 'receipt': receipt})))
        for row in receipt:
            if row['status'] != 'ok':
                continue
            for source in row.get('sources', [row]):
                kind = source.get('kind', 'content')
                identity = (analysis_id, source['location_id'], source['workspace_id'], source['path'],
                            source.get('version') or '', kind)
                source_id = db.execute('''INSERT INTO analysis_sources(
                    analysis_id,location_id,workspace_id,path,version,kind,total) VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(analysis_id,location_id,workspace_id,path,version,kind)
                    DO UPDATE SET total=excluded.total RETURNING id''',
                    (*identity, source.get('total'))).fetchone()[0]
                if kind == 'content':
                    db.execute('''INSERT OR IGNORE INTO analysis_ranges(source_id,batch_id,unit,start,end)
                        VALUES(?,?,?,?,?)''', (source_id, note.batch_id, row['unit'], row['start'], row['end']))
        db.execute("UPDATE analysis_batches SET state='committed', note_digest=? WHERE id=?",
                   (checksum, note.batch_id))

    def commit(self, analysis_id, note):
        with self.store.tx() as db:
            self._commit(db, analysis_id, note)

    def plan(self, analysis_id, call_id, request):
        plan_json = encode(request.model_dump(exclude={'previous_note'}))
        with self.store.tx() as db:
            if request.previous_note:
                self._commit(db, analysis_id, request.previous_note)
            existing = db.execute('SELECT * FROM analysis_batches WHERE analysis_id=? AND call_id=?',
                                  (analysis_id, call_id)).fetchone()
            pending = db.execute("""SELECT * FROM analysis_batches WHERE analysis_id=?
                AND state!='committed' ORDER BY ordinal LIMIT 1""", (analysis_id,)).fetchone()
            if existing or pending:
                return dict(existing or pending)
            number = db.execute('SELECT COALESCE(MAX(ordinal),0)+1 FROM analysis_batches WHERE analysis_id=?',
                                (analysis_id,)).fetchone()[0]
            batch_id = uid()
            db.execute('INSERT INTO analysis_batches(id,analysis_id,ordinal,call_id,plan_json) VALUES(?,?,?,?,?)',
                       (batch_id, analysis_id, number, call_id, plan_json))
            return dict(db.execute('SELECT * FROM analysis_batches WHERE id=?', (batch_id,)).fetchone())

    def returned(self, batch_id, rows, payload):
        with self.store.tx() as db:
            db.execute("""UPDATE analysis_batches SET state='returned',receipt_json=?,receipt_digest=?
                WHERE id=? AND state!='committed'""", (encode(rows), digest(payload), batch_id))

    def presented(self, analysis_id, payload):
        with self.store.tx() as db:
            db.execute("""UPDATE analysis_batches SET state='presented' WHERE id=? AND analysis_id=?
                AND receipt_digest=? AND state='returned'""",
                (payload['batch_id'], analysis_id, digest(payload)))

    def notes(self, analysis_id, after=0, limit=12):
        return self.store.query('''SELECT batch_id,seq,body_json FROM analysis_notes WHERE analysis_id=?
            AND seq>? ORDER BY seq LIMIT ?''', (analysis_id, after, limit))

    def pending(self, analysis_id):
        rows = self.store.query("""SELECT id,ordinal,state,plan_json FROM analysis_batches WHERE analysis_id=?
            AND state!='committed' ORDER BY ordinal LIMIT 1""", (analysis_id,))
        return rows[0] if rows else None
