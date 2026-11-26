import os
import httpx
from .models import IndexUnavailable, SearchHit


class SearchProtocolError(RuntimeError):
    pass


class SearchClient:
    def __init__(self, url=None, token=None, *, timeout=None, transport=None):
        url = url or os.getenv('ARTORIAS_SEARCH_URL', 'http://127.0.0.1:8766')
        token = token or os.getenv('ARTORIAS_SEARCH_TOKEN')
        if not token:
            raise ValueError('ARTORIAS_SEARCH_TOKEN is required for remote search')
        timeout = float(timeout if timeout is not None else os.getenv('ARTORIAS_SEARCH_TIMEOUT', '3'))
        if timeout <= 0:
            raise ValueError('Search timeout must be positive')
        self.http = httpx.Client(base_url=url, headers={'Authorization': f'Bearer {token}'},
                                 timeout=timeout, transport=transport, trust_env=False)

    def close(self):
        self.http.close()

    def request(self, endpoint, payload):
        try:
            response = self.http.post(endpoint, json=payload)
        except httpx.TransportError as exc:
            raise IndexUnavailable('Search transport unavailable') from exc
        if response.status_code >= 500 or response.status_code in (408, 429):
            raise IndexUnavailable(f'Search temporarily unavailable: {response.status_code}')
        if response.status_code == 409 and response.json().get('detail') == 'index_unavailable':
            raise IndexUnavailable('Search index requires snapshot')
        if response.status_code >= 400:
            raise SearchProtocolError(f'Search rejected request: {response.status_code} {response.text[:200]}')
        return response.json()

    def status(self, collection, projection_version):
        return self.request('/v1/status', dict(collection=collection, projection_version=projection_version))

    def send(self, event):
        result = self.request('/v1/events', event)
        if result.get('ack') != event['seq']:
            raise SearchProtocolError('Invalid search acknowledgement')

    def search(self, scope, query, *, projection_version, mode='any', limit=20):
        result = self.request('/v1/search', dict(collection=scope.collection, partition=scope.partition,
                              query=query, projection_version=projection_version, mode=mode, limit=limit))
        return [SearchHit(**hit) for hit in result['hits']]
