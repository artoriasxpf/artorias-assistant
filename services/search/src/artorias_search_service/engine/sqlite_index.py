"""Transactional SQLite FTS5 adapter; no business model or server dependency."""
from collections.abc import Iterable
from contextlib import contextmanager
import hashlib
import json
import logging
import sqlite3
from time import perf_counter

from .models import Document, IndexUnavailable, Mode, Scope, SearchHit, TextTokenizer, WriteResult
from .tokenizer import Tokenizer

log = logging.getLogger(__name__)
FORMAT_VERSION = 'fts5-hex-sha256-v1'


def _term(value: str) -> str:
    # Short terms use compact reversible encoding; long identifiers use a digest
    # to stay below FTS token limits. Both are safe alphanumeric MATCH literals.
    encoded = value.encode('utf-8')
    return 'x' + encoded.hex() if len(encoded) <= 32 else 'h' + hashlib.sha256(encoded).hexdigest()


@contextmanager
def _index_errors():
    try:
        yield
    except sqlite3.OperationalError as exc:
        message = str(exc)
        if message == 'no such module: fts5' or message.startswith('no such table: text_search_'):
            raise IndexUnavailable(message) from exc
        raise  # Busy, disk-full, malformed SQL and unrelated DB errors are not no-match.


class SQLiteTextIndex:
    def __init__(self, tokenizer: TextTokenizer | None = None, *, max_query_terms: int = 32,
                 max_results: int = 100, max_query_chars: int = 30000):
        if min(max_query_terms, max_results, max_query_chars) < 1:
            raise ValueError('Search limits must be positive')
        self.tokenizer = tokenizer if tokenizer is not None else Tokenizer()
        self.max_query_terms, self.max_results, self.max_query_chars = max_query_terms, max_results, max_query_chars

    @property
    def version(self):
        return f'{self.tokenizer.version}/{FORMAT_VERSION}'

    def initialize(self, conn: sqlite3.Connection) -> None:
        with _index_errors():
            had_fts = conn.execute("SELECT 1 FROM sqlite_master WHERE name='text_search_fts'").fetchone()
            had_docs = conn.execute("SELECT 1 FROM sqlite_master WHERE name='text_search_documents'").fetchone()
            conn.execute('''CREATE TABLE IF NOT EXISTS text_search_collections(
                collection TEXT PRIMARY KEY, tokenizer_version TEXT NOT NULL,
                projection_version TEXT NOT NULL, built_at TEXT NOT NULL)''')
            conn.execute('''CREATE TABLE IF NOT EXISTS text_search_documents(
                rowid INTEGER PRIMARY KEY, collection TEXT NOT NULL, scope TEXT NOT NULL,
                document_id TEXT NOT NULL, title TEXT NOT NULL, keywords TEXT NOT NULL,
                body TEXT NOT NULL, content_hash TEXT NOT NULL,
                UNIQUE(collection, scope, document_id))''')
            conn.execute('''CREATE VIRTUAL TABLE IF NOT EXISTS text_search_fts USING fts5(
                scope_token, title_terms, keyword_terms, body_terms, tokenize='ascii')''')
            if not had_fts or not had_docs:
                conn.execute('DELETE FROM text_search_collections')
                # Discard orphaned FTS entries if the companion document table was lost.
                if not had_docs:
                    conn.execute('DELETE FROM text_search_fts')

    def is_current(self, conn: sqlite3.Connection, collection: str, projection_version: str) -> bool:
        with _index_errors():
            row = conn.execute('''SELECT tokenizer_version,projection_version
                FROM text_search_collections WHERE collection=?''', (collection,)).fetchone()
            return row is not None and tuple(row) == (self.version, projection_version)

    def _require_current(self, conn, collection, projection_version):
        if not self.is_current(conn, collection, projection_version):
            raise IndexUnavailable('Index version mismatch or collection not built')

    @staticmethod
    def _scope(scope: Scope) -> str:
        return _term(json.dumps([scope.collection, scope.partition], ensure_ascii=False))

    def _upsert(self, conn, scope, document, projection_version):
        values = (document.title, document.keywords, document.body)
        fingerprint = hashlib.sha256(json.dumps((self.version, projection_version, *values),
                                               ensure_ascii=False).encode('utf-8')).hexdigest()
        params = (scope.collection, scope.partition, document.id)
        row = conn.execute('''SELECT rowid,content_hash FROM text_search_documents
            WHERE collection=? AND scope=? AND document_id=?''', params).fetchone()
        if row is not None and row[1] == fingerprint:
            return False
        fields = [' '.join(_term(term) for term in self.tokenizer.tokenize(text, for_index=True)) for text in values]
        if row is None:
            rowid = conn.execute('''INSERT INTO text_search_documents
                (collection,scope,document_id,title,keywords,body,content_hash) VALUES(?,?,?,?,?,?,?)''',
                (*params, *values, fingerprint)).lastrowid
        else:
            rowid = row[0]
            conn.execute('''UPDATE text_search_documents SET title=?,keywords=?,body=?,content_hash=?
                WHERE rowid=?''', (*values, fingerprint, rowid))
            conn.execute('DELETE FROM text_search_fts WHERE rowid=?', (rowid,))
        conn.execute('''INSERT INTO text_search_fts(rowid,scope_token,title_terms,keyword_terms,body_terms)
            VALUES(?,?,?,?,?)''', (rowid, self._scope(scope), *fields))
        return True

    def upsert(self, conn: sqlite3.Connection, scope: Scope, documents: Iterable[Document], *,
               projection_version: str) -> WriteResult:
        started = perf_counter()
        with _index_errors():
            self._require_current(conn, scope.collection, projection_version)
            updated = skipped = 0
            for document in documents:
                if self._upsert(conn, scope, document, projection_version):
                    updated += 1
                else:
                    skipped += 1
        log.debug('search.upsert collection=%s updated=%d skipped=%d version=%s/%s ms=%.2f',
                  scope.collection, updated, skipped, self.version, projection_version,
                  (perf_counter() - started) * 1000)
        return WriteResult(updated, skipped)

    def delete(self, conn: sqlite3.Connection, scope: Scope, document_ids: Iterable[str]) -> int:
        count = 0
        with _index_errors():
            for document_id in document_ids:
                row = conn.execute('''DELETE FROM text_search_documents
                    WHERE collection=? AND scope=? AND document_id=? RETURNING rowid''',
                    (scope.collection, scope.partition, document_id)).fetchone()
                if row is not None:
                    conn.execute('DELETE FROM text_search_fts WHERE rowid=?', (row[0],))
                    count += 1
        return count

    def rebuild(self, conn: sqlite3.Connection, collection: str,
                documents: Iterable[tuple[str, Document]], *, projection_version: str) -> int:
        """Replace a whole collection, streamed from the authoritative source.

        Caller MUST roll back the transaction on exceptions. Version is published last.
        """
        with _index_errors():
            conn.execute('''DELETE FROM text_search_fts WHERE rowid IN
                (SELECT rowid FROM text_search_documents WHERE collection=?)''', (collection,))
            conn.execute('DELETE FROM text_search_documents WHERE collection=?', (collection,))
            count = 0
            for partition, document in documents:
                count += self._upsert(conn, Scope(collection, partition), document, projection_version)
            conn.execute('''INSERT INTO text_search_collections
                (collection,tokenizer_version,projection_version,built_at)
                VALUES(?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                ON CONFLICT(collection) DO UPDATE SET tokenizer_version=excluded.tokenizer_version,
                projection_version=excluded.projection_version,built_at=excluded.built_at''',
                (collection, self.version, projection_version))
        log.info('search.rebuild collection=%s documents=%d version=%s/%s',
                 collection, count, self.version, projection_version)
        return count

    def search(self, conn: sqlite3.Connection, scope: Scope, query: str, *, projection_version: str,
               limit: int = 20, mode: Mode = 'any') -> list[SearchHit]:
        if not 1 <= limit <= self.max_results or mode not in ('any', 'all') or len(query) > self.max_query_chars:
            raise ValueError('Invalid query length, match mode or result limit')
        started = perf_counter()
        with _index_errors():
            self._require_current(conn, scope.collection, projection_version)
            words = list(dict.fromkeys(self.tokenizer.tokenize(query)))[:self.max_query_terms]
            if not words:
                return []
            operator = ' AND ' if mode == 'all' else ' OR '
            expression = f'scope_token:{self._scope(scope)} AND {{title_terms keyword_terms body_terms}}:('
            expression += operator.join(_term(word) for word in words) + ')'
            rows = conn.execute('''SELECT doc.document_id,bm25(text_search_fts,0.0,5.0,2.5,1.0) AS rank
                FROM text_search_fts JOIN text_search_documents AS doc ON doc.rowid=text_search_fts.rowid
                WHERE text_search_fts MATCH ? AND doc.collection=? AND doc.scope=?
                ORDER BY rank,doc.document_id LIMIT ?''', (expression, scope.collection, scope.partition, limit))
            hits = [SearchHit(row[0], row[1]) for row in rows]
        log.debug('search.query collection=%s hits=%d version=%s/%s ms=%.2f',
                  scope.collection, len(hits), self.version, projection_version,
                  (perf_counter() - started) * 1000)
        return hits
