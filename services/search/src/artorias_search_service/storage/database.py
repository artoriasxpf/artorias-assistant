import sqlite3
from contextlib import closing, contextmanager


class SearchDatabase:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as conn:
            conn.execute('PRAGMA journal_mode=WAL')

    @contextmanager
    def transaction(self, *, write=False):
        with closing(sqlite3.connect(self.path, timeout=5)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield conn
