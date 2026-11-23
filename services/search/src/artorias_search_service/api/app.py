"""Internal authenticated HTTP boundary."""
import secrets

from fastapi import Depends, FastAPI, Header, HTTPException

from ..config import load_config
from ..engine.sqlite_index import SQLiteTextIndex
from ..storage.database import SearchDatabase
from ..storage.migrations import migrate
from .routes import create_router


def create_app(path=None, *, token=None, collections=None, index=None):
    path, token, allowed = load_config(path, token, collections)
    database = SearchDatabase(path)
    index = index or SQLiteTextIndex()
    migrate(database, index)

    def authenticate(authorization: str = Header(default='')):
        if not secrets.compare_digest(authorization, f'Bearer {token}'):
            raise HTTPException(401, 'Invalid service credentials')

    app = FastAPI(title='Artorias Search', dependencies=[Depends(authenticate)])

    @app.get('/healthz')
    def health():
        return {'status': 'ok'}

    @app.get('/readyz')
    def ready():
        with database.transaction() as conn:
            conn.execute('SELECT rowid FROM text_search_fts LIMIT 1').fetchone()
        return {'status': 'ready'}

    app.include_router(create_router(database, index, allowed))
    return app
