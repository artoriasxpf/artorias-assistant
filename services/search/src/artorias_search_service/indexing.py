"""Apply ordered index events and persist the cursor atomically."""
from .engine.models import Document, IndexUnavailable, Scope


class EventConflict(RuntimeError):
    pass


def apply_event(database, index, event):
    with database.transaction(write=True) as conn:
        consumer = conn.execute('SELECT * FROM search_consumers WHERE collection=?',
                                (event.collection,)).fetchone()
        if consumer and consumer['source_id'] != event.source_id:
            raise EventConflict('source_conflict')
        last = consumer['last_seq'] if consumer else 0
        if event.seq <= last:
            return {'ack': event.seq, 'duplicate': True}
        if event.operation != 'snapshot' and (consumer is None or event.seq != last + 1):
            raise EventConflict('sequence_gap')
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
