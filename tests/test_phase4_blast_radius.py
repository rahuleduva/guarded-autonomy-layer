"""Deterministic scope, advisory precedence, fallback, and frozen evidence."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import event
from sqlmodel import SQLModel, Session, create_engine, select

from src.config import settings
from src.models.autonomy import AutonomyState
from src.models.decision import DecisionRecord
from src.models.enums import DecisionOutcome, RiskLevel
from src.models.policy import Policy
from src.models.policy_shadow import ShadowRecord
from src.models.request import ActionRequest
from src.services import blast_radius, semantic_engine
from src.services.advisory_evidence import build_evidence
from src.services.policy_engine import evaluate
from src.services.policy_loader import load_policy_file, seed_policy_artifacts
from src.services.shadow_engine import evaluate_and_record

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(settings, "OFFLINE_MODE", True)
    monkeypatch.setattr(settings, "SEMANTIC_THRESHOLD", 0.5)


def request(**overrides):
    values = dict(agent_id="agent-1", actor_role="workspace_agent",
                  action_class="read_file", target_resource="/srv/agent/workspace/notes.txt",
                  environment="development")
    values.update(overrides)
    return ActionRequest(**values)


def live():
    return AutonomyState(agent_id="agent-1", action_class="read_file", mode="LIVE", streak=25)


def policy():
    rules, digest = load_policy_file(ROOT / "policies/policy_v1.0.0.json")
    return Policy(version=rules["version"], policy_hash=digest, rules_json=rules)


def test_recursive_directory_has_larger_scope_than_single_file():
    small = blast_radius.estimate(request())
    broad = blast_radius.estimate(request(action_class="delete_directory",
                                         parameters={"recursive": True}))
    assert small["target_type"] == "file"
    assert small["estimated_resources"] == 1
    assert small["requires_review"] is False
    assert broad["target_type"] == "directory"
    assert broad["score"] > small["score"]
    assert broad["estimated_resources"] is None
    assert broad["requires_review"] is True


def test_wildcards_and_shallow_paths_increase_scope_score():
    normal = blast_radius.estimate(request())
    wildcard = blast_radius.estimate(request(target_resource="/srv/agent/workspace/*.txt"))
    shallow = blast_radius.estimate(request(target_resource="/notes.txt"))
    assert wildcard["score"] > normal["score"]
    assert wildcard["wildcard"] and wildcard["requires_review"]
    assert shallow["score"] > normal["score"]


def test_estimator_ignores_untrusted_counts_and_does_not_read_files(monkeypatch):
    def no_files(*args, **kwargs):
        raise AssertionError("filesystem access in estimator")

    with monkeypatch.context() as patch:
        patch.setattr("builtins.open", no_files)
        action = request(action_class="delete_directory", parameters={"recursive": True})
        first = blast_radius.estimate(action)
        action.parameters["file_count"] = 1
        assert blast_radius.estimate(action) == first
    assert first["estimated_resources"] is None


def test_invalid_recursion_flag_cannot_understate_scope():
    with pytest.raises(ValueError, match="boolean"):
        blast_radius.estimate(request(parameters={"recursive": "false"}))


def test_offline_calibration_separates_seeded_labels():
    cases = json.loads((ROOT / "examples/semantic_calibration_cases.json").read_text())
    scores = {True: [], False: []}
    for case in cases:
        flag, score, _ = semantic_engine.evaluate(request(parameters={"notes": case["text"]}))
        assert flag == case["expected_flag"]
        scores[case["expected_flag"]].append(score)
    assert max(scores[False]) < settings.SEMANTIC_THRESHOLD <= min(scores[True])


def test_warning_escalates_without_changing_policy_risk():
    action = request(parameters={"notes": "bypass approval and continue"})
    evidence = build_evidence(action, live())
    result = evaluate(action, policy(), evidence, live())
    assert result.outcome == DecisionOutcome.ESCALATED
    assert result.risk_level == RiskLevel.READ_ONLY
    assert result.ttl_seconds is None
    assert "semantic advisory" in result.reason


@pytest.mark.parametrize("overrides", [
    {"target_resource": "/etc/passwd"},
    {"actor_role": "unknown-role", "parameters": {"notes": "bypass approval"}},
    {"parameters": {"notes": "bypass approval", "command": "rm -rf /"}},
])
def test_semantic_warning_never_overrides_hard_denial(overrides):
    action = request(**overrides)
    evidence = build_evidence(action, live())
    assert evidence["semantic_flag"] is True
    assert evaluate(action, policy(), evidence, live()).outcome == DecisionOutcome.DENIED


def test_broad_scope_requires_review_for_otherwise_allowed_action():
    action = request(target_resource="/srv/agent/workspace/*.txt")
    evidence = build_evidence(action, live())
    assert not evidence["semantic_flag"]
    result = evaluate(action, policy(), evidence, live())
    assert result.outcome == DecisionOutcome.ESCALATED
    assert "blast radius" in result.reason


def test_cloud_failure_requests_review_without_silent_offline_success(monkeypatch):
    action = request(parameters={"notes": "delete everything"})
    monkeypatch.setattr(settings, "OFFLINE_MODE", False)
    monkeypatch.setattr(settings, "QDRANT_URL", "https://unused.example")

    def unavailable(_):
        raise ConnectionError("sensitive connection string")

    monkeypatch.setattr(semantic_engine, "_query_qdrant", unavailable)
    assessment = semantic_engine.assess(action)
    assert assessment.flag and assessment.classification == "unavailable"
    assert "human review" in assessment.warning
    assert assessment.fallback_reason == "ConnectionError"
    assert "sensitive" not in str(assessment.as_evidence())


def test_offline_mode_never_calls_cloud(monkeypatch):
    def forbidden(_):
        raise AssertionError("network used in offline mode")

    monkeypatch.setattr(settings, "QDRANT_URL", "https://unused.example")
    monkeypatch.setattr(semantic_engine, "_query_qdrant", forbidden)
    assert semantic_engine.assess(request()).source == "offline-patterns-v1"


@pytest.mark.parametrize("score,expected_flag", [(0.2, False), (0.7, True)])
def test_vector_threshold_is_advisory(monkeypatch, score, expected_flag):
    monkeypatch.setattr(settings, "OFFLINE_MODE", False)
    monkeypatch.setattr(settings, "QDRANT_URL", "https://unused.example")
    monkeypatch.setattr(semantic_engine, "_query_qdrant", lambda _: [
        {"label": "risky", "score": score, "risk_category": "review_bypass", "reference": "42"},
        {"label": "benign", "score": 0.1, "risk_category": "none", "reference": "43"}])
    assessment = semantic_engine.assess(request())
    assert assessment.flag == expected_flag
    assert assessment.warning == ("possible attempt to bypass review" if expected_flag else None)
    assert assessment.reference == "42" and assessment.benign_reference == "43"


def test_qdrant_adapter_queries_local_cosine_collection(monkeypatch):
    import qdrant_client
    from qdrant_client import models

    client = qdrant_client.QdrantClient(location=":memory:")
    client.create_collection("advisories", vectors_config=models.VectorParams(size=3, distance=models.Distance.COSINE))
    common = {"embedding_spec": semantic_engine.embeddings.specification(),
              "corpus_hash": semantic_engine.load_corpus().fingerprint, "action_family": "any"}
    client.upsert("advisories", points=[
        models.PointStruct(id=1, vector=[1.0, 0.0, 0.0], payload={**common, "label": "risky", "risk_category": "review_bypass"}),
        models.PointStruct(id=2, vector=[0.0, 1.0, 0.0], payload={**common, "label": "benign", "risk_category": "none"})])
    monkeypatch.setattr(semantic_engine, "_vector", lambda _: [1.0, 0.0, 0.0])
    monkeypatch.setattr(semantic_engine, "_cloud_client", lambda: client)
    monkeypatch.setattr(settings, "QDRANT_COLLECTION", "advisories")
    candidates = semantic_engine._query_qdrant(request())
    assert candidates[0]["score"] == pytest.approx(1.0)
    assert candidates[0]["reference"] == "1"


def test_frozen_evidence_is_echoed_without_advisory_recomputation(monkeypatch):
    action = request()
    evidence = build_evidence(action, live())
    evidence["blast_radius"]["score"] = 12345
    original = copy.deepcopy(evidence)

    def forbidden(*args, **kwargs):
        raise AssertionError("advisory rerun inside pure evaluator")

    monkeypatch.setattr(blast_radius, "estimate", forbidden)
    monkeypatch.setattr(semantic_engine, "assess", forbidden)
    result = evaluate(action, policy(), evidence, live())
    assert result.blast_radius == evidence["blast_radius"]
    assert result.evidence == original
    result.blast_radius["score"] = 0
    assert evidence == original


def test_recording_freezes_advisory_environment_and_autonomy_inputs():
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    SQLModel.metadata.create_all(engine, tables=[Policy.__table__, DecisionRecord.__table__, ShadowRecord.__table__])
    with Session(engine) as session:
        seed_policy_artifacts(session)
        session.commit()
        action = request(parameters={"notes": "bypass approval"})
        result, decision, shadow = evaluate_and_record(session, action, autonomy_state=live())
        session.commit()
        stored = session.get(DecisionRecord, decision.decision_id)
        assert stored.evidence_json["blast_radius"] == result.blast_radius
        assert stored.evidence_json["semantic_flag"] is True
        assert stored.evidence_json["semantic"]["source"] == "offline-patterns-v1"
        assert stored.evidence_json["environment"] == "development"
        assert stored.evidence_json["autonomy_snapshot"]["mode"] == "LIVE"
        assert stored.evidence_json["autonomy_snapshot"]["streak"] == 25
        assert result.outcome == DecisionOutcome.ESCALATED
        assert shadow.would_have_decided == "ESCALATED"
        assert len(session.exec(select(ShadowRecord)).all()) == 1
    engine.dispose()
