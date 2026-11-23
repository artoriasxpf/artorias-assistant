"""Internal authenticated HTTP boundary."""
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

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

    app = FastAPI(title='Artorias Search')
    protected = APIRouter(dependencies=[Depends(authenticate)])

    @protected.get('/healthz')
    def health():
        return {'status': 'ok'}

    @protected.get('/readyz')
    def ready():
        with database.transaction() as conn:
            conn.execute('SELECT rowid FROM text_search_fts LIMIT 1').fetchone()
        return {'status': 'ready'}

    protected.include_router(create_router(database, index, allowed))
    app.include_router(protected)

    admin_dir = Path(__file__).resolve().parents[1] / 'admin'

    @app.get('/admin', include_in_schema=False)
    def admin():
        return FileResponse(admin_dir / 'index.html', headers={
            'Cache-Control': 'no-store',
            'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; "
                                       "connect-src 'self'; base-uri 'none'; form-action 'none'; "
                                       "frame-ancestors 'none'",
            'Referrer-Policy': 'no-referrer',
            'X-Content-Type-Options': 'nosniff',
        })

    app.mount('/admin/assets', StaticFiles(directory=admin_dir), name='admin-assets')
    return app
