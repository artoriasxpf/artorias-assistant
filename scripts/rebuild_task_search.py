#!/usr/bin/env python3
"""Rebuild the lexical Task index from business_tasks; no model or file reads."""
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
    count = TaskRepository(Store(path)).rebuild_search()
    print(f'Rebuilt {count} Task documents in {path}')


if __name__ == '__main__':
    main()
