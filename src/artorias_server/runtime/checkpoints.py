"""Local checkpoint adapter. A cloud deployment must inject shared storage."""
from pathlib import Path
import asyncio
import sqlite3
from contextlib import asynccontextmanager
from artorias_server.storage.migrations import ensure_timestamps
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

class SQLiteCheckpointProvider:
    def __init__(self, path):
        self.path = Path(path)

    @asynccontextmanager
    async def open(self):
        async with AsyncSqliteSaver.from_conn_string(str(self.path)) as saver:
            await saver.setup()
            def migrate():
                with sqlite3.connect(self.path) as connection:
                    connection.execute('BEGIN IMMEDIATE')
                    ensure_timestamps(connection)
            await asyncio.to_thread(migrate)
            yield saver
