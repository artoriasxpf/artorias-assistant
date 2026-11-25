"""Session permissions and operation-bound approval records."""
import hashlib
import json
from contextvars import ContextVar
from pathlib import Path

from artorias_shared.ids import uid

tool_call = ContextVar("permission_tool_call", default=None)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class MissingPermission(PermissionError):
    def __init__(self, resource):
        super().__init__('目标未授权；请使用 project_authorize 请求访问权限')
        self.resource = resource


class Permissions:
    def __init__(self, store):
        self.store = store
        with store.tx() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS permission_grants(
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, location_id TEXT NOT NULL,
                    workspace_id TEXT, action TEXT NOT NULL CHECK(action IN ('read','write','execute')),
                    resource_kind TEXT NOT NULL CHECK(resource_kind IN ('file','directory','command')),
                    root TEXT, rule_hash TEXT, rule_json TEXT,
                    source_task_id TEXT, source_run_id TEXT, source_request_id TEXT UNIQUE,
                    revoked_at TEXT,
                    CHECK ((resource_kind='command' AND action='execute' AND rule_hash IS NOT NULL
                            AND rule_json IS NOT NULL AND root IS NULL)
                        OR (resource_kind!='command' AND action!='execute' AND root IS NOT NULL
                            AND rule_hash IS NULL AND rule_json IS NULL))
                );
                CREATE INDEX IF NOT EXISTS active_session_grants
                    ON permission_grants(session_id,location_id,root,rule_hash) WHERE revoked_at IS NULL;
                CREATE TABLE IF NOT EXISTS permission_requests(
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, run_id TEXT NOT NULL,
                    task_id TEXT, tool_call_id TEXT NOT NULL, operation_hash TEXT NOT NULL,
                    resource_json TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending'
                        CHECK(state IN ('pending','approved','rejected','consumed','cancelled')),
                    choice TEXT, grant_id TEXT,
                    UNIQUE(run_id,tool_call_id,operation_hash)
                );
            """)

    def matching(self, session, resource):
        candidates = self.store.query(
            """SELECT * FROM permission_grants WHERE session_id=? AND location_id=?
               AND revoked_at IS NULL AND (workspace_id IS NULL OR workspace_id=?)""",
            (session, resource['location_id'], resource.get('workspace_id')),
        )
        matches = [grant for grant in candidates if covers(grant, resource)]
        return max(matches, key=lambda grant: len(Path(grant['root'] or '.').parts),
                   default=None)

    def request(self, ws, call_id, resource, operation):
        operation_hash = fingerprint([resource, operation])
        with self.store.tx() as db:
            if db.execute(
                "SELECT 1 FROM permission_requests WHERE run_id=? AND operation_hash=? AND state='rejected'",
                (ws.run, operation_hash),
            ).fetchone():
                return None
            db.execute(
                """INSERT OR IGNORE INTO permission_requests
                   (id,session_id,run_id,task_id,tool_call_id,operation_hash,resource_json)
                   VALUES (?,?,?,?,?,?,?)""",
                (uid(), ws.session, ws.run, ws.task, call_id, operation_hash, json.dumps(resource)),
            )
            return db.execute(
                'SELECT id FROM permission_requests WHERE run_id=? AND tool_call_id=? AND operation_hash=?',
                (ws.run, call_id, operation_hash),
            ).fetchone()[0]

    def claim(self, request_id):
        with self.store.tx() as db:
            row = db.execute('SELECT * FROM permission_requests WHERE id=?', (request_id,)).fetchone()
            if row['state'] != 'approved':
                return False
            if row['choice'] == 'remember':
                return 'remember' if db.execute(
                    'SELECT 1 FROM permission_grants WHERE id=? AND revoked_at IS NULL',
                    (row['grant_id'],),
                ).fetchone() else False
            db.execute("UPDATE permission_requests SET state='consumed' WHERE id=?", (request_id,))
            return 'approve'


def covers(grant, resource):
    if grant['resource_kind'] == 'command' or resource['resource_kind'] == 'command':
        return (grant['resource_kind'] == resource['resource_kind']
                and grant['rule_hash'] == resource['rule_hash']
                and json.loads(grant['rule_json']) == resource['rule'])
    if resource['action'] == 'write' and grant['action'] != 'write':
        return False
    root, target = Path(grant['root']), Path(resource['root'])
    if grant['resource_kind'] == 'file':
        return resource['resource_kind'] == 'file' and target == root
    return target == root or root in target.parents


def record_decision(db, request_id, session, run, choice):
    row = db.execute(
        "SELECT * FROM permission_requests WHERE id=? AND session_id=? AND run_id=? AND state='pending'",
        (request_id, session, run),
    ).fetchone()
    if not row:
        raise ValueError('权限申请已处理或不存在')
    grant_id = None
    if choice == 'remember':
        resource = json.loads(row['resource_json'])
        grant_id = uid()
        db.execute(
            """INSERT INTO permission_grants
               (id,session_id,location_id,workspace_id,action,resource_kind,root,rule_hash,
                rule_json,source_task_id,source_run_id,source_request_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (grant_id, session, resource['location_id'], resource.get('workspace_id'),
             resource['action'], resource['resource_kind'], resource.get('root'), resource.get('rule_hash'),
             json.dumps(resource['rule']) if 'rule' in resource else None, row['task_id'], run, request_id),
        )
    state = 'rejected' if choice == 'reject' else 'approved'
    db.execute('UPDATE permission_requests SET state=?,choice=?,grant_id=? WHERE id=?',
               (state, choice, grant_id, request_id))
