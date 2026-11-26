import os
from pathlib import Path

DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 8766


def load_config(path, token, collections):
    token = token or os.getenv('ARTORIAS_SEARCH_TOKEN')
    if not token:
        raise ValueError('ARTORIAS_SEARCH_TOKEN is required')
    allowed = set(collections if collections is not None else
                  os.getenv('ARTORIAS_SEARCH_COLLECTIONS', 'tasks').split(','))
    path = Path(path or os.getenv('ARTORIAS_SEARCH_DB', 'search-data/search.sqlite')).resolve()
    return path, token, allowed
