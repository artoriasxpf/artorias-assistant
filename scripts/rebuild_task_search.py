#!/usr/bin/env python3
"""Rebuild the lexical Task index from tasks; no model or file reads."""
import argparse
import os
from pathlib import Path

from dotenv import load_dotenv
from artorias_server.storage.sqlite import Store
from artorias_server.tasks.repository import TaskRepository


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, help='Data directory (default: ARTORIAS_DATA or ./data)')
    args = parser.parse_args()
    path = (args.data or Path(os.getenv('ARTORIAS_DATA', 'data'))).resolve() / 'app.sqlite'
    if not path.is_file():
        parser.error(f'Existing database required: {path}')
    repo = TaskRepository(Store(path))
    count = repo.rebuild_search()
    if getattr(repo.search.index, 'remote', False):
        from artorias_server.integrations.search.worker import SearchSyncWorker
        try:
            worker = SearchSyncWorker(repo.search)
            while worker.sync_once():
                pass
        finally:
            repo.search.index.client.close()
    print(f'Rebuilt {count} Task documents from {path}')


if __name__ == '__main__':
    main()
