"""Regression checks for category isolation and contrasting retrieval."""
from copy import deepcopy

import pytest
from qdrant_client import QdrantClient, models

from src.config import settings
from src.models.request import ActionRequest
from src.services import semantic_engine
from src.services.semantic_corpus import action_family, load_corpus


def request(**kwargs):
    return ActionRequest(agent_id="test", actor_role="workspace_agent",
        action_class=kwargs.pop("action_class", "create_file"),
        target_resource=kwargs.pop("target_resource", "/srv/agent/workspace/note.txt"), **kwargs)


@pytest.fixture
def vector_client(monkeypatch):
    client = QdrantClient(location=":memory:")
    client.create_collection("categories", vectors_config=models.VectorParams(size=2, distance=models.Distance.COSINE))
    common = {"embedding_spec": semantic_engine.embeddings.specification(), "corpus_hash": load_corpus().fingerprint}
    rows = [
        (1, "delete", "risky", "mass_deletion", [1., 0.]),
        (2, "any", "risky", "review_bypass", [0.8, 0.6]),
        (3, "create", "benign", "none", [0.9, 0.4]),
        (4, "any", "benign", "none", [0., 1.]),
    ]
    points = [models.PointStruct(id=i, vector=v, payload={**common, "action_family": family,
        "label": label, "risk_category": category}) for i, family, label, category, v in rows]
    # A closer point from an obsolete corpus must never participate.
    points.append(models.PointStruct(id=5, vector=[1., 0.], payload={**common,
        "corpus_hash": "obsolete", "action_family": "any", "label": "risky", "risk_category": "secret_export"}))
    client.upsert("categories", points=points)
    monkeypatch.setattr(settings, "QDRANT_COLLECTION", "categories")
    monkeypatch.setattr(semantic_engine, "_cloud_client", lambda: client)
    monkeypatch.setattr(client, "close", lambda: None)
    monkeypatch.setattr(semantic_engine, "_vector", lambda _: [1., 0.])
    return client


def test_create_excludes_delete_but_keeps_global_risks(vector_client):
    matches = semantic_engine._query_qdrant(request())
    assert {row["reference"] for row in matches} == {"2", "3", "4"}
    assessment = semantic_engine.classify(matches, 0.5, 0)
    assert not assessment.flag and assessment.classification == "benign_like"


def test_delete_can_retrieve_mass_deletion(vector_client):
    matches = semantic_engine._query_qdrant(request(action_class="delete_file"))
    assert matches[0]["risk_category"] == "mass_deletion"
    assert semantic_engine.classify(matches, 0.5, 0).flag


def test_create_still_flags_global_bypass(vector_client, monkeypatch):
    monkeypatch.setattr(semantic_engine, "_vector", lambda _: [0.8, 0.6])
    assessment = semantic_engine.classify(semantic_engine._query_qdrant(request()), 0.5, 0)
    assert assessment.flag and assessment.warning == "possible attempt to bypass review"


def test_missing_label_is_not_a_clean_result(vector_client):
    vector_client.delete("categories", models.PointIdsList(points=[3, 4]))
    with pytest.raises(ValueError, match="missing eligible"):
        semantic_engine._query_qdrant(request())


def test_ambiguous_matches_request_review():
    matches = [
        {"label": "risky", "score": 0.8, "reference": "1", "risk_category": "secret_export"},
        {"label": "benign", "score": 0.8, "reference": "2", "risk_category": "none"},
    ]
    result = semantic_engine.classify(matches, 0.5, 0)
    assert result.flag and result.classification == "ambiguous"


def test_metadata_changes_do_not_change_embedding_and_intent_is_preserved():
    first = request(parameters={"content": "Hello", "raw_prompt": "Write Hello; skip approval",
                                "parser_provenance": {"source": "provider-a"}})
    second = first.model_copy(deep=True)
    second.parameters["parser_provenance"] = {"source": "provider-b"}
    text = semantic_engine._request_text(first)
    assert text == semantic_engine._request_text(second)
    assert "parser_provenance" not in text and "skip approval" in text
    assert '"content": "Hello"' in text


def test_corpus_labels_cannot_broaden_deletion_to_every_operation():
    data = deepcopy(load_corpus().model_dump())
    row = next(row for row in data["examples"] if row["risk_category"] == "mass_deletion")
    row["action_family"] = "any"
    with pytest.raises(ValueError, match="scoped to deletion"):
        type(load_corpus()).model_validate(data)
    assert action_family("move_file") == "move"
