"""Durable event replay and live SSE polling, independent of worker lifetime."""
import asyncio
import json

async def stream_events(store, session, request, after=0):
    cursor = after
    while not await request.is_disconnected():
        rows = store.events(session, cursor)
        for row in rows:
            cursor = row['seq']
            row['payload'] = json.loads(row['payload'])
            yield f'id: {cursor}\ndata: {json.dumps(row, ensure_ascii=False)}\n\n'
        if not rows:
            yield ': heartbeat\n\n'
            await asyncio.sleep(.5)
