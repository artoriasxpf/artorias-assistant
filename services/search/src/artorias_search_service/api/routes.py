from dataclasses import asdict

from fastapi import APIRouter, HTTPException

from ..engine.models import IndexUnavailable, Scope
from ..indexing import EventConflict, apply_event
from .schemas import CollectionRequest, Event, EventResponse, SearchRequest, SearchResponse, StatusResponse


def create_router(database, index, allowed):
    router = APIRouter()

    def authorize(collection):
        if collection not in allowed:
            raise HTTPException(403, 'Collection not allowed')

    @router.post('/v1/status', response_model=StatusResponse)
    def status(request: CollectionRequest):
        authorize(request.collection)
        with database.transaction() as conn:
            consumer = conn.execute('SELECT source_id,last_seq FROM search_consumers WHERE collection=?',
                                    (request.collection,)).fetchone()
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE name IN ('text_search_documents','text_search_fts','text_search_collections')")}
            current = len(tables) == 3 and index.is_current(conn, request.collection, request.projection_version)
            return {'current': current,
                    'tokenizer_version': index.version, 'consumer': dict(consumer) if consumer else None}

    @router.post('/v1/search', response_model=SearchResponse)
    def search(request: SearchRequest):
        authorize(request.collection)
        try:
            with database.transaction() as conn:
                hits = index.search(conn, Scope(request.collection, request.partition), request.query,
                                    projection_version=request.projection_version,
                                    mode=request.mode, limit=request.limit)
            return {'hits': [asdict(hit) for hit in hits]}
        except IndexUnavailable as exc:
            raise HTTPException(409, 'index_unavailable') from exc

    @router.post('/v1/events', response_model=EventResponse)
    def apply(event: Event):
        authorize(event.collection)
        try:
            return apply_event(database, index, event)
        except (EventConflict, IndexUnavailable) as exc:
            detail = str(exc) if isinstance(exc, EventConflict) else 'index_unavailable'
            raise HTTPException(409, detail) from exc

    return router
