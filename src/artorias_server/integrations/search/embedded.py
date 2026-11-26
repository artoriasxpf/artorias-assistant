"""Optional compatibility adapter for the in-process index."""
try:
    from artorias_search_service.engine.models import IndexUnavailable as EngineUnavailable
    from artorias_search_service.engine.sqlite_index import SQLiteTextIndex
except ModuleNotFoundError as exc:
    if exc.name != 'artorias_search_service':
        raise
    raise RuntimeError('Embedded search requires uv sync --extra search') from exc

from .models import IndexUnavailable


class EmbeddedTextIndex(SQLiteTextIndex):
    def initialize(self, conn):
        try:
            return super().initialize(conn)
        except EngineUnavailable as exc:
            raise IndexUnavailable(str(exc)) from exc

    def search(self, conn, scope, query, **kwargs):
        try:
            return super().search(conn, scope, query, **kwargs)
        except EngineUnavailable as exc:
            raise IndexUnavailable(str(exc)) from exc
