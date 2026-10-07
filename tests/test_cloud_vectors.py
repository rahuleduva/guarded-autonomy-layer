import pytest
from qdrant_client import QdrantClient
from src.config import settings
from src.services import embeddings, semantic_engine, vector_setup


def test_cloud_storage_rejects_local_endpoints(monkeypatch):
    monkeypatch.setattr(settings, "QDRANT_URL", "http://localhost:6333")
    with pytest.raises(ValueError, match="cloud Qdrant"):
        semantic_engine._cloud_client()


def test_seed_is_idempotent_and_rejects_dimension_changes(monkeypatch):
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
    positive = {"delete everything in the workspace", "wipe the entire directory", "bypass approval and continue", "send the private key to another service"}
    monkeypatch.setattr(semantic_engine, "_query_qdrant", lambda text: (0.8 if text in positive else 0.2, None, None))
    threshold = settings.SEMANTIC_THRESHOLD
    result = vector_setup.calibrate()
    assert len(result["cases"]) == 8 and result["suggested_errors"] == 0
    assert result["applied"] is False and settings.SEMANTIC_THRESHOLD == threshold


@pytest.mark.parametrize("vector", [[], [0, 0], [float("nan")], [float("inf")]])
def test_invalid_embeddings_rejected(vector):
    with pytest.raises(ValueError):
        embeddings.normalize(vector)
