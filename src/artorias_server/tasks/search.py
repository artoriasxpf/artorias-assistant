"""Task projections and candidate fusion; lexical mechanics live in artorias_search."""
import logging
import re
import os

from artorias_search import Document, IndexUnavailable, Scope, TextIndex
from .requirements import requirements_for

log = logging.getLogger(__name__)
COLLECTION = 'tasks'
PROJECTION_VERSION = 'task-goal-rules-summary-12000-v1'
_TASK_ID = re.compile(r'(?<![a-zA-Z0-9_])[a-fA-F0-9]{32}(?![a-zA-Z0-9_])')


def project(task) -> Document:
    requirements = requirements_for(task)
    body = task['summary']
    if len(body) > 12000:
        body = body[:5999] + '\n\n' + body[-5999:]
    return Document(task['id'], task['goal'], '\n'.join([
        *requirements['constraints'].values(), *requirements['criteria'].values(),
    ]), body)


class TaskSearch:
    def __init__(self, store, index: TextIndex | None = None):
        self.store = store
        if index is not None:
            self.index = index
        elif os.getenv('ARTORIAS_SEARCH_MODE', 'remote') == 'embedded':
            from artorias_search import SQLiteTextIndex
            self.index = SQLiteTextIndex()
        elif os.getenv('ARTORIAS_SEARCH_MODE', 'remote') == 'remote':
            from artorias_search_client import OutboxTextIndex, SearchClient
            self.index = OutboxTextIndex(SearchClient())
        else:
            raise ValueError('ARTORIAS_SEARCH_MODE must be remote or embedded')

    def prepare(self, *, force=False) -> int | None:
        with self.store.tx() as conn:
            self.index.initialize(conn)
            if force or not self.index.is_current(conn, COLLECTION, PROJECTION_VERSION):
                def documents():
                    cursor = conn.execute('SELECT * FROM business_tasks ORDER BY rowid')
                    while rows := cursor.fetchmany(200):
                        for row in rows:
                            yield row['session'], project(row)
                return self.index.rebuild(conn, COLLECTION, documents(), projection_version=PROJECTION_VERSION)
        return None

    def update(self, conn, task_id):
        row = conn.execute('SELECT * FROM business_tasks WHERE id=?', (task_id,)).fetchone()
        return self.index.upsert(conn, Scope(COLLECTION, row['session']), [project(row)],
                                 projection_version=PROJECTION_VERSION)

    def candidates(self, session, text):
        # Fetch remotely before opening the local transaction; hydrate current authority later.
        remote = getattr(self.index, 'remote', False)
        remote_hits = self._keyword_hits(None, session, text) if remote else None
        with self.store.tx() as conn:
            explicit = []
            for task_id in dict.fromkeys(match.group().lower() for match in _TASK_ID.finditer(text)):
                if conn.execute('SELECT 1 FROM business_tasks WHERE session=? AND id=?',
                                (session, task_id)).fetchone():
                    explicit.append(task_id)
                    if len(explicit) == 20:
                        break
            hits = remote_hits if remote else self._keyword_hits(conn, session, text)
            recent = conn.execute('''SELECT id FROM business_tasks WHERE session=?
                ORDER BY updated_at DESC,rowid DESC LIMIT 8''', (session,)).fetchall()
            sources = {}
            for source, ids in (('explicit', explicit), ('keyword', [hit.id for hit in hits]),
                                ('recent', [row[0] for row in recent])):
                for task_id in ids:
                    labels = sources.setdefault(task_id, [])
                    if source not in labels:
                        labels.append(source)
            candidates = []
            for task_id, labels in sources.items():
                row = conn.execute('SELECT * FROM business_tasks WHERE session=? AND id=?',
                                   (session, task_id)).fetchone()
                if row is None:
                    continue
                candidate = {key: row[key] for key in ('id', 'goal', 'status', 'revision', 'source_task', 'updated_at')}
                candidate['summary'] = row['summary'][-2000:]
                candidate['effective_requirements'] = requirements_for(row)
                candidate['recall_sources'] = labels
                candidates.append(candidate)
                if len(candidates) == 20:
                    break
            return candidates

    def _keyword_hits(self, conn, session, text):
        try:
            return self.index.search(conn, Scope(COLLECTION, session), text[:30000],
                                     projection_version=PROJECTION_VERSION, limit=10)
        except IndexUnavailable as exc:
            log.warning('task.recall degraded: %s', exc)
            return []
