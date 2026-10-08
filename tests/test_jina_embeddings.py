import math
from types import SimpleNamespace

import httpx
import pytest

from src.config import settings
from src.services import embeddings, semantic_engine


@pytest.fixture
def jina(monkeypatch):
    monkeypatch.setattr(settings, 'OFFLINE_MODE', False)
    monkeypatch.setattr(settings, 'EMBEDDING_PROVIDER', 'jina')
    monkeypatch.setattr(settings, 'JINA_API_KEY', 'test-key')
    monkeypatch.setattr(settings, 'JINA_EMBEDDING_DIMENSIONS', 2)
    monkeypatch.setattr(settings, 'REDIS_URL', '')
    calls = []
    response = SimpleNamespace(status_code=200, json=lambda: {'data': [{'index': 0, 'embedding': [3, 4]}]})
    class Client:
        def __init__(self, **kwargs):
            assert kwargs['follow_redirects'] is False
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def post(self, url, **kwargs):
            calls.append(kwargs)
            return response
    monkeypatch.setattr(httpx, 'Client', Client)
    return calls, response


def test_dispatch_and_shared_key(jina):
    calls, _ = jina
    assert semantic_engine._vector('sample') == pytest.approx([0.6, 0.8])
    assert calls[0]['headers']['Authorization'] == 'Bearer test-key'
    assert calls[0]['json']['task'] == 'text-matching'
    assert embeddings.specification().startswith('jina:')


@pytest.mark.parametrize('vector', [[1], [0, 0], [math.nan, 1], ['1', 2]])
def test_invalid_vectors(jina, vector):
    _, response = jina
    response.json = lambda: {'data': [{'index': 0, 'embedding': vector}]}
    with pytest.raises(ValueError, match='invalid vector'):
        embeddings.jina_embed('sample')


def test_http_error(jina):
    _, response = jina
    response.status_code = 400
    with pytest.raises(ValueError, match='HTTP 400'):
        embeddings.jina_embed('sample')
