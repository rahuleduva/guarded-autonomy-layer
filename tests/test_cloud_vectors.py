import pytest
import json
from qdrant_client import QdrantClient
from src.config import settings, PROJECT_ROOT
from src.services import embeddings, semantic_engine, vector_setup


def test_cloud_storage_rejects_local_endpoints(monkeypatch):
    monkeypatch.setattr(settings, "QDRANT_URL", "http://localhost:6333")
    with pytest.raises(ValueError, match="cloud Qdrant"):
        semantic_engine._cloud_client()


def test_seed_is_idempotent_and_rejects_dimension_changes(monkeypatch):
    monkeypatch.setattr(vector_setup.time, "sleep", lambda _: None)
    client = QdrantClient(location=":memory:")
    monkeypatch.setattr(settings, "OFFLINE_MODE", False)
    monkeypatch.setattr(settings, "QDRANT_COLLECTION", "test-patterns")
    monkeypatch.setattr(semantic_engine, "_cloud_client", lambda: client)
    monkeypatch.setattr(semantic_engine, "_vector", lambda _: [1.0, 0.0, 0.0])
    monkeypatch.setattr(client, "close", lambda: None)
    first = vector_setup.seed()
    vector_setup.seed()
    assert client.count("test-patterns", exact=True).count == first["seeded_patterns"]
    monkeypatch.setattr(semantic_engine, "_vector", lambda _: [1.0, 0.0])
    with pytest.raises(ValueError, match="dimensions"):
        vector_setup.seed()
    assert client.count("test-patterns", exact=True).count == first["seeded_patterns"]


def test_calibration_measures_without_changing_settings(monkeypatch):
    monkeypatch.setattr(settings, "OFFLINE_MODE", False)
    monkeypatch.setattr(vector_setup.time, "sleep", lambda _: None)
    cases = json.loads((PROJECT_ROOT / "examples/semantic_action_validation_cases.json").read_text())
    labels = {semantic_engine._request_text(vector_setup.example_request(row["request"])): row["expected_flag"] for row in cases}
    def matches(request):
        return [{"label": "risky", "score": 0.8 if labels[semantic_engine._request_text(request)] else 0.2,
                 "risk_category": "review_bypass", "reference": "risk"},
                {"label": "benign", "score": 0.3, "risk_category": "none", "reference": "benign"}]
    monkeypatch.setattr(semantic_engine, "_query_qdrant", matches)
    threshold = settings.SEMANTIC_THRESHOLD
    result = vector_setup.calibrate()
    assert len(result["cases"]) == 20
    assert result["calibration"]["suggested"]["errors"] == 0
    assert result["holdout"]["suggested"]["errors"] == 0
    assert result["applied"] is False and settings.SEMANTIC_THRESHOLD == threshold


@pytest.mark.parametrize("vector", [[], [0, 0], [float("nan")], [float("inf")]])
def test_invalid_embeddings_rejected(vector):
    with pytest.raises(ValueError):
        embeddings.normalize(vector)
