import os

from ..client import SearchClient
from ..models import Scope, TextIndex
from ..outbox import OutboxTextIndex
from .projection import COLLECTION, PROJECTION_VERSION, project
from .recall import recall_candidates


class TaskSearch:
    def __init__(self, store, index: TextIndex | None = None):
        self.store = store
        if index is not None:
            self.index = index
        elif os.getenv('ARTORIAS_SEARCH_MODE', 'remote') == 'embedded':
            from ..embedded import EmbeddedTextIndex
            self.index = EmbeddedTextIndex()
        elif os.getenv('ARTORIAS_SEARCH_MODE', 'remote') == 'remote':
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
        return recall_candidates(self.store, self.index, session, text)
