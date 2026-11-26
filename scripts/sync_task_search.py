#!/usr/bin/env python3
"""Run the durable Task search delivery worker separately from runtime pods."""
import argparse
import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from artorias_server.storage.sqlite import Store
from artorias_server.tasks.repository import TaskRepository
from artorias_server.integrations.search.worker import SearchSyncWorker


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path)
    parser.add_argument('--once', action='store_true', help='Drain current backlog, exit nonzero on failure')
    args = parser.parse_args()
    path = (args.data or Path(os.getenv('ARTORIAS_DATA', 'data'))) / 'app.sqlite'
    if not path.is_file():
        parser.error('Existing app.sqlite required')
    repo = TaskRepository(Store(path))
    if not getattr(repo.search.index, 'remote', False):
        parser.error('Remote search mode required')
    worker = SearchSyncWorker(repo.search)
    logging.basicConfig(level=logging.INFO)
    try:
        if args.once:
            while worker.sync_once():
                pass
        else:
            asyncio.run(worker.run())
    finally:
        repo.search.index.client.close()


if __name__ == '__main__':
    main()
