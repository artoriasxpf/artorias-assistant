"""Durable ordered delivery; HTTP never runs while holding a business transaction."""
import asyncio
import json
import logging

from artorias_search_client.client import SearchProtocolError
from .search import COLLECTION, PROJECTION_VERSION

log = logging.getLogger(__name__)


class SearchSyncWorker:
    def __init__(self, search, *, poll_seconds=1.0):
        self.search = search
        self.store = search.store
        self.client = search.index.client
        self.poll_seconds = poll_seconds

    def sync_once(self):
        status = self.client.status(COLLECTION, PROJECTION_VERSION)
        state = self.store.query('SELECT * FROM search_sync_state WHERE collection=?', (COLLECTION,))[0]
        consumer = status['consumer']
        if consumer and consumer['source_id'] != state['source_id']:
            raise SearchProtocolError('Search collection is owned by another source database')
        if consumer and consumer['last_seq'] > state['next_seq']:
            raise SearchProtocolError('Business database is older than search cursor; restore/rebuild service database')
        rows = self.store.query('SELECT seq,payload FROM search_outbox WHERE collection=? ORDER BY seq LIMIT 1',
                                (COLLECTION,))
        if not status['current'] and (not rows or json.loads(rows[0]['payload'])['operation'] != 'snapshot'):
            self.search.prepare(force=True)
            rows = self.store.query('SELECT seq,payload FROM search_outbox WHERE collection=? ORDER BY seq LIMIT 1',
                                    (COLLECTION,))
        if not rows:
            return False
        event = json.loads(rows[0]['payload'])
        self.client.send(event)
        # A crash here causes a duplicate delivery, which the service acknowledges safely.
        with self.store.tx() as conn:
            conn.execute('DELETE FROM search_outbox WHERE collection=? AND seq<=?', (COLLECTION, event['seq']))
        return True

    async def run(self):
        delay = self.poll_seconds
        while True:
            try:
                operation = asyncio.create_task(asyncio.to_thread(self.sync_once))
                try:
                    progressed = await asyncio.shield(operation)
                except asyncio.CancelledError:
                    # Finish the bounded HTTP operation before the application closes its client.
                    try:
                        await operation
                    except Exception:
                        pass
                    raise
                delay = self.poll_seconds
                if progressed:
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Do not discard poison events or skip ahead. Operators repair configuration/data.
                log.warning('task.search_sync retry error_type=%s delay=%s', type(exc).__name__, delay)
                delay = min(max(delay * 2, 1), 60)
            await asyncio.sleep(delay)
