from types import SimpleNamespace

import httpx
import pytest

from src.config import settings
from src.services import embeddings
from src.services.llm_routing import current_routing


def test_catalog_keeps_current_gemini_specification(monkeypatch):
    assert embeddings.specification() == 'gemini:gemini-embedding-2:768:similarity-v1'
    assert current_routing().embeddings.models['aicredits_bge_m3'].model == 'baai/bge-m3'


def test_selected_profile_is_validated():
    routing = current_routing().model_copy(deep=True)
    routing.embeddings.selected = 'unknown'
    with pytest.raises(ValueError, match='selected embedding profile is missing'):
        type(routing).model_validate(routing.model_dump())


def test_aicredits_embedding_uses_selected_catalog_model(monkeypatch):
    monkeypatch.setattr(settings, 'OFFLINE_MODE', False)
    routing = current_routing().model_copy(deep=True)
    routing.embeddings.selected = 'aicredits_bge_m3'
    monkeypatch.setattr(embeddings, 'current_routing', lambda: routing)
    monkeypatch.setattr(settings, 'AI_CREDITS_API_KEY', 'test-key')
    monkeypatch.setattr(settings, 'AI_CREDITS_API_URL', 'https://example.invalid/v1')
    calls = []

    class Client:
        def __init__(self, **kwargs):
            assert kwargs['follow_redirects'] is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return SimpleNamespace(raise_for_status=lambda: None,
                                   json=lambda: {'data': [{'index': 0, 'embedding': [3, 4]}]})

    monkeypatch.setattr(httpx, 'Client', Client)
    assert embeddings.aicredits_embed('sample') == pytest.approx([0.6, 0.8])
    assert calls[0][0] == 'https://example.invalid/v1/embeddings'
    assert calls[0][1]['json'] == {'model': 'baai/bge-m3', 'input': 'sample'}
