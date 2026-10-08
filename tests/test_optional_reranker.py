"""Jina is optional, with validated results and nonfatal failure behavior."""
from copy import deepcopy

import httpx
import pytest

from src.config import settings
from src.models.request import ActionRequest
from src.services import reranker, semantic_engine


@pytest.fixture
def candidates():
    return [
        {"reference": "risk", "label": "risky", "risk_category": "review_bypass",
         "score": 0.7, "text": "Bypass required approval."},
        {"reference": "safe", "label": "benign", "risk_category": "none",
         "score": 0.9, "text": "Follow the approval process."},
    ]


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setattr(settings, "OFFLINE_MODE", False)
    monkeypatch.setattr(settings, "JINA_RERANKER_API_KEY", "synthetic-test-key")
    monkeypatch.setattr(settings, "JINA_RERANKER_API_URL", "https://example.test/v1/rerank")
    monkeypatch.setattr(settings, "JINA_RERANKER_MODE", "observe")
    captured = {}
    class Client:
        def __init__(self, **kwargs):
            captured.update(kwargs)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def post(self, url, **kwargs):
            captured.update(kwargs)
            if "exception" in captured:
                raise captured["exception"]
            return httpx.Response(captured.get("status", 200), json=captured.get("body", {
                "results": [{"index": 1, "relevance_score": 0.2},
                            {"index": 0, "relevance_score": 0.8}]}))
    monkeypatch.setattr(reranker.httpx, "Client", Client)
    return captured


def test_success_maps_document_indices_without_mutating_cosine_scores(provider, candidates):
    original = deepcopy(candidates)
    result = reranker.rerank("Assess this request", candidates)
    assert result["status"] == "applied" and result["preferred_label"] == "risky"
    assert result["ranked"][0]["reference"] == "risk"
    assert candidates == original
    assert provider["json"]["top_n"] == 2
    assert provider["follow_redirects"] is False


@pytest.mark.parametrize("status", [400, 401, 429, 500])
def test_http_failure_is_nonfatal(provider, candidates, status):
    provider["status"] = status
    result = reranker.rerank("query", candidates)
    assert result["status"] == "skipped" and result["reason"] == f"http_{status}"


def test_timeout_is_nonfatal_and_does_not_expose_exception_message(provider, candidates):
    provider["exception"] = httpx.ReadTimeout("sensitive content")
    result = reranker.rerank("query", candidates)
    assert result["reason"] == "ReadTimeout" and "sensitive" not in str(result)


@pytest.mark.parametrize("results", [
    [], [{"index": 0, "relevance_score": 0.4}] * 2,
    [{"index": 4, "relevance_score": 0.4}, {"index": 1, "relevance_score": 0.3}],
    [{"index": 0, "relevance_score": "0.5"}, {"index": 1, "relevance_score": 0.3}],
])
def test_partial_or_malformed_results_are_rejected(provider, candidates, results):
    provider["body"] = {"results": results}
    assert reranker.rerank("query", candidates)["reason"] == "invalid_response"


def test_missing_key_never_calls_provider(provider, candidates, monkeypatch):
    monkeypatch.setattr(settings, "JINA_RERANKER_API_KEY", "")
    assert reranker.rerank("query", candidates)["reason"] == "not_configured"
    assert "json" not in provider


@pytest.mark.parametrize("mode,status,flag", [("observe", 200, False), ("review", 200, True), ("review", 400, False)])
def test_assessment_preserves_baseline_on_failure_and_observation(provider, candidates, monkeypatch, mode, status, flag):
    monkeypatch.setattr(settings, "JINA_RERANKER_MODE", mode)
    monkeypatch.setattr(settings, "SEMANTIC_THRESHOLD", 0.5)
    monkeypatch.setattr(settings, "SEMANTIC_SCORE_MARGIN", 0.0)
    monkeypatch.setattr(semantic_engine, "_query_qdrant", lambda _: candidates)
    provider["status"] = status
    result = semantic_engine.assess(ActionRequest(agent_id="test", actor_role="workspace_agent",
        action_class="create_file", target_resource="note.txt", parameters={"content": "Hello"}))
    assert result.flag == flag and result.source.startswith("qdrant:")
    assert result.reranking["status"] == ("applied" if status == 200 else "skipped")
