import pytest


@pytest.fixture(autouse=True)
def embedded_search_for_unit_tests(monkeypatch):
    """Existing unit tests exercise transactional indexing without external services.

    Remote integration tests explicitly inject OutboxTextIndex or opt into remote mode.
    """
    monkeypatch.setenv('ARTORIAS_SEARCH_MODE', 'embedded')
