"""Internal authenticated HTTP boundary; owns its database and transactions."""
import os
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from artorias_search.contracts import Document, IndexUnavailable, Scope
from artorias_search.index import SQLiteTextIndex


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class CollectionRequest(StrictModel):
    collection: str = Field(min_length=1, max_length=200)
    projection_version: str = Field(min_length=1, max_length=200)


class SearchRequest(CollectionRequest):
    partition: str = Field(min_length=1, max_length=500)
    query: str = Field(max_length=30000)
    mode: Literal['any', 'all'] = 'any'
    limit: int = Field(default=20, ge=1, le=100)


class IndexedDocument(StrictModel):
    id: str = Field(min_length=1, max_length=500)
    title: str = ''
    keywords: str = ''
    body: str = ''


class Entry(StrictModel):
    partition: str = Field(min_length=1, max_length=500)
    document: IndexedDocument


class Event(CollectionRequest):
    source_id: str = Field(min_length=1, max_length=200)
    seq: int = Field(ge=1)
    operation: Literal['snapshot', 'upsert', 'delete']
    entries: list[Entry] = Field(default_factory=list)


def create_app(path=None, *, token=None, collections=None, index=None):
    token = token or os.getenv('ARTORIAS_SEARCH_TOKEN')
    if not token:
        raise ValueError('ARTORIAS_SEARCH_TOKEN is required')
    allowed = set(collections if collections is not None else
                  os.getenv('ARTORIAS_SEARCH_COLLECTIONS', 'tasks').split(','))
    path = Path(path or os.getenv('ARTORIAS_SEARCH_DB', 'search-data/search.sqlite')).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    index = index or SQLiteTextIndex()

    @contextmanager
    def transaction(*, write=False):
        conn = sqlite3.connect(path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    with sqlite3.connect(path) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
    with transaction(write=True) as conn:
        index.initialize(conn)
        conn.execute('''CREATE TABLE IF NOT EXISTS search_consumers(
            collection TEXT PRIMARY KEY, source_id TEXT NOT NULL, last_seq INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))''')

    def authenticate(authorization: str = Header(default='')):
        if not secrets.compare_digest(authorization, f'Bearer {token}'):
            raise HTTPException(401, 'Invalid service credentials')

    def authorize(collection):
        if collection not in allowed:
            raise HTTPException(403, 'Collection not allowed')

    app = FastAPI(title='Artorias Search', dependencies=[Depends(authenticate)])

    @app.get('/healthz')
    def health():
        return {'status': 'ok'}

    @app.get('/readyz')
    def ready():
        with transaction() as conn:
            conn.execute('SELECT rowid FROM text_search_fts LIMIT 1').fetchone()
        return {'status': 'ready'}

    @app.post('/v1/status')
    def status(request: CollectionRequest):
        authorize(request.collection)
        with transaction() as conn:
            consumer = conn.execute('SELECT source_id,last_seq FROM search_consumers WHERE collection=?',
                                    (request.collection,)).fetchone()
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE name IN ('text_search_documents','text_search_fts','text_search_collections')")}
            current = len(tables) == 3 and index.is_current(conn, request.collection, request.projection_version)
            return {'current': current,
                    'tokenizer_version': index.version, 'consumer': dict(consumer) if consumer else None}

    @app.post('/v1/search')
    def search(request: SearchRequest):
        authorize(request.collection)
        try:
            with transaction() as conn:
                hits = index.search(conn, Scope(request.collection, request.partition), request.query,
                                    projection_version=request.projection_version,
                                    mode=request.mode, limit=request.limit)
            return {'hits': [asdict(hit) for hit in hits]}
        except IndexUnavailable as exc:
            raise HTTPException(409, 'index_unavailable') from exc

    @app.post('/v1/events')
    def apply(event: Event):
        authorize(event.collection)
        try:
            with transaction(write=True) as conn:
                consumer = conn.execute('SELECT * FROM search_consumers WHERE collection=?',
                                        (event.collection,)).fetchone()
                if consumer and consumer['source_id'] != event.source_id:
                    raise HTTPException(409, 'source_conflict')
                last = consumer['last_seq'] if consumer else 0
                if event.seq <= last:
                    return {'ack': event.seq, 'duplicate': True}
                if event.operation != 'snapshot' and (consumer is None or event.seq != last + 1):
                    raise HTTPException(409, 'sequence_gap')
                if event.operation == 'snapshot':
                    # Re-initialize also repairs a missing FTS companion table.
                    index.initialize(conn)
                    index.rebuild(conn, event.collection,
                                  ((entry.partition, Document(**entry.document.model_dump()))
                                   for entry in event.entries), projection_version=event.projection_version)
                else:
                    for entry in event.entries:
                        scope = Scope(event.collection, entry.partition)
                        if event.operation == 'upsert':
                            index.upsert(conn, scope, [Document(**entry.document.model_dump())],
                                         projection_version=event.projection_version)
                        else:
                            if not index.is_current(conn, event.collection, event.projection_version):
                                raise IndexUnavailable('Version mismatch')
                            index.delete(conn, scope, [entry.document.id])
                conn.execute('''INSERT INTO search_consumers(collection,source_id,last_seq) VALUES(?,?,?)
                    ON CONFLICT(collection) DO UPDATE SET last_seq=excluded.last_seq,
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')''',
                    (event.collection, event.source_id, event.seq))
                return {'ack': event.seq, 'duplicate': False}
        except IndexUnavailable as exc:
            raise HTTPException(409, 'index_unavailable') from exc

    return app
