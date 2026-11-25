"""Device-owned SQLite operation journal, usable without importing the server.

The all-in-one profile points this adapter at the existing app.sqlite to retain
atomic file journal/event commits and compatibility. A standalone device may
use its own database; network replication of these records is not implemented.
"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class SQLiteWorkspaceJournal:
    def __init__(self, path):
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.tx() as c:
            for statement in (
                'CREATE TABLE IF NOT EXISTS workspace_bindings(session TEXT PRIMARY KEY, root TEXT NOT NULL)',
                '''CREATE TABLE IF NOT EXISTS workspace_operations(
                    id TEXT PRIMARY KEY, session TEXT, run TEXT, root TEXT, path TEXT,
                    before_hash TEXT, after_hash TEXT, before_mode INTEGER, after_mode INTEGER,
                    reverts TEXT, kind TEXT, status TEXT)''',
                'CREATE TABLE IF NOT EXISTS changes(id TEXT PRIMARY KEY, session TEXT, run TEXT, path TEXT, before_hash TEXT, after_hash TEXT, reverts TEXT)',
                'CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT, run TEXT, kind TEXT, payload TEXT)',
                '''CREATE TABLE IF NOT EXISTS resource_changes(change_id TEXT PRIMARY KEY,
                    task_id TEXT, workspace_id TEXT, location_id TEXT NOT NULL, backend TEXT NOT NULL,
                    root TEXT NOT NULL, blob_root TEXT NOT NULL)''',
            ):
                c.execute(statement)
            # Only this adapter's ordinary tables. Never inspect/alter FTS internals.
            now = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
            for table in ('workspace_bindings','workspace_operations','changes','events','resource_changes'):
                columns = {r[1] for r in c.execute(f'PRAGMA table_info({table})')}
                for field in ('created_at','updated_at'):
                    if field not in columns:
                        c.execute(f"ALTER TABLE {table} ADD COLUMN {field} TEXT NOT NULL DEFAULT ''")
                c.execute(f"UPDATE {table} SET created_at={now} WHERE created_at=''")
                c.execute(f"UPDATE {table} SET updated_at=created_at WHERE updated_at=''")
                c.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_timestamps_insert AFTER INSERT ON {table}
                    BEGIN UPDATE {table} SET created_at=CASE WHEN NEW.created_at='' THEN {now} ELSE NEW.created_at END,
                    updated_at=CASE WHEN NEW.updated_at='' THEN {now} ELSE NEW.updated_at END WHERE rowid=NEW.rowid; END''')
                c.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_timestamps_update AFTER UPDATE ON {table}
                    WHEN NEW.updated_at=OLD.updated_at AND OLD.created_at<>''
                    BEGIN UPDATE {table} SET created_at=OLD.created_at,updated_at={now} WHERE rowid=NEW.rowid; END''')

    @property
    def data_root(self):
        return str(Path(self.path).parent)

    @contextmanager
    def tx(self):
        c = sqlite3.connect(self.path, timeout=5)
        c.row_factory = sqlite3.Row
        try:
            c.execute('BEGIN IMMEDIATE')
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def _query(self, sql, args=()):
        with self.tx() as c:
            return [dict(r) for r in c.execute(sql, args)]

    def binding(self, session):
        rows = self._query('SELECT root FROM workspace_bindings WHERE session=?', (session,))
        return rows[0]['root'] if rows else None

    def bind(self, session, root):
        with self.tx() as c:
            c.execute('INSERT OR IGNORE INTO workspace_bindings(session,root) VALUES(?,?)', (session,root))
            return c.execute('SELECT root FROM workspace_bindings WHERE session=?',(session,)).fetchone()[0]

    def pending(self, root=None):
        sql = "SELECT * FROM workspace_operations WHERE status='pending'"
        return self._query(sql + (' AND root=?' if root is not None else ''), (root,) if root is not None else ())

    def prepare(self, op, resource):
        with self.tx() as c:
            if resource:
                c.execute('INSERT INTO resource_changes(change_id,backend,location_id,root,blob_root,task_id,workspace_id) VALUES(?,?,?,?,?,?,?)',
                          (op['id'],resource['backend'],resource['location_id'],op['root'],resource['blob_root'],resource.get('task_id'),resource.get('workspace_id')))
            keys = ('id','root','session','run','path','before_hash','after_hash','before_mode','after_mode','reverts','kind','status')
            c.execute('INSERT INTO workspace_operations('+','.join(keys)+') VALUES('+','.join('?' for _ in keys)+')', tuple(op[k] for k in keys))

    def finish(self, op):
        with self.tx() as c:
            changed = c.execute("UPDATE workspace_operations SET status='committed' WHERE id=? AND status='pending'", (op['id'],))
            if not changed.rowcount:
                return
            keys = ('id','session','run','path','before_hash','after_hash','reverts')
            c.execute('INSERT OR IGNORE INTO changes('+','.join(keys)+') VALUES(?,?,?,?,?,?,?)', tuple(op[k] for k in keys))
            # File mutations are already visible as tool.call/tool.result events, so no
            # file.* rows are written to the session event table; the changes table
            # remains the durable ledger for revert and audit queries.

    def abort(self, operation_id):
        with self.tx() as c:
            c.execute("UPDATE workspace_operations SET status='aborted' WHERE id=? AND status='pending'",(operation_id,))

    def committed(self, change_id, session, root):
        rows = self._query("SELECT * FROM workspace_operations WHERE id=? AND session=? AND root=? AND status='committed'",(change_id,session,root))
        return rows[0] if rows else None

    def find_change(self, change_id, session_id):
        rows = self._query('SELECT o.*,r.backend,r.location_id,r.blob_root,r.task_id,r.workspace_id FROM workspace_operations o LEFT JOIN resource_changes r ON r.change_id=o.id WHERE o.id=? AND o.session=?', (change_id,session_id))
        return rows[0] if rows else None

    def list_changes(self, session_id):
        return self._query('SELECT * FROM changes WHERE session=? ORDER BY rowid DESC', (session_id,))
