"""TextIndex-compatible writer. Local methods only enqueue; search is remote."""
from dataclasses import asdict
import json
import uuid

from .models import WriteResult


class OutboxTextIndex:
    remote = True

    def __init__(self, client):
        self.client = client

    def initialize(self, conn):
        conn.execute('''CREATE TABLE IF NOT EXISTS search_sync_state(
            collection TEXT PRIMARY KEY, source_id TEXT NOT NULL,
            projection_version TEXT NOT NULL, next_seq INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))''')
        conn.execute('''CREATE TABLE IF NOT EXISTS search_outbox(
            collection TEXT NOT NULL, seq INTEGER NOT NULL, payload TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            PRIMARY KEY(collection,seq))''')

    def is_current(self, conn, collection, projection_version):
        row = conn.execute('SELECT projection_version FROM search_sync_state WHERE collection=?',
                           (collection,)).fetchone()
        return row is not None and row[0] == projection_version

    def _enqueue(self, conn, collection, version, operation, entries):
        row = conn.execute('''UPDATE search_sync_state SET next_seq=next_seq+1,
            updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE collection=?
            RETURNING source_id,next_seq''', (collection,)).fetchone()
        if row is None:
            raise RuntimeError('Search outbox not initialized')
        payload = dict(collection=collection, projection_version=version, source_id=row[0],
                       seq=row[1], operation=operation, entries=entries)
        conn.execute('INSERT INTO search_outbox(collection,seq,payload) VALUES(?,?,?)',
                     (collection, row[1], json.dumps(payload, ensure_ascii=False)))

    def rebuild(self, conn, collection, documents, *, projection_version):
        entries = [dict(partition=partition, document=asdict(doc)) for partition, doc in documents]
        conn.execute('''INSERT INTO search_sync_state(collection,source_id,projection_version)
            VALUES(?,?,?) ON CONFLICT(collection) DO UPDATE SET projection_version=excluded.projection_version''',
            (collection, uuid.uuid4().hex, projection_version))
        # A complete authoritative snapshot supersedes pending deltas. Never reset sequence.
        conn.execute('DELETE FROM search_outbox WHERE collection=?', (collection,))
        self._enqueue(conn, collection, projection_version, 'snapshot', entries)
        return len(entries)

    def upsert(self, conn, scope, documents, *, projection_version):
        if not self.is_current(conn, scope.collection, projection_version):
            raise RuntimeError('Outbox projection version mismatch; prepare first')
        entries = [dict(partition=scope.partition, document=asdict(doc)) for doc in documents]
        self._enqueue(conn, scope.collection, projection_version, 'upsert', entries)
        return WriteResult(updated=len(entries), skipped=0)

    def delete(self, conn, scope, document_ids):
        row = conn.execute('SELECT projection_version FROM search_sync_state WHERE collection=?',
                           (scope.collection,)).fetchone()
        entries = [dict(partition=scope.partition, document={'id': value}) for value in document_ids]
        self._enqueue(conn, scope.collection, row[0], 'delete', entries)
        return len(entries)  # Queued identifiers, not a remote affected-row count.

    def search(self, conn, scope, query, **kwargs):
        if conn is not None:
            raise RuntimeError('Remote search must run outside the business transaction')
        return self.client.search(scope, query, **kwargs)
