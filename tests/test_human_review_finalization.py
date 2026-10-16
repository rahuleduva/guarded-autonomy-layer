"""Deterministic review API regressions; concurrency is checked on PostgreSQL.

These tests use a migrated temporary database and never call live providers.
"""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import event
from sqlmodel import Session, select

from src.config import settings
from src.evaluation import demo_session, propose
from src.main import create_app
from src.models.decision import DecisionRecord
from src.models.escalation import EscalationQueueItem
from src.models.human_review import HumanReviewRecord
from src.models.human_review import ReviewExplanationOutput
from src.models.policy import Policy
from src.models.request import ApprovalDecision
from src.services import autonomy_service, escalation_service, review_explanation
from src.services.policy_store import activate


@pytest.fixture
def review_session(monkeypatch):
    monkeypatch.setattr(settings, "SEMANTIC_PIPELINE_MODE", "baseline")
    monkeypatch.setattr(settings, "DEV_DIAGNOSTICS", False)
    def unavailable(*args, **kwargs):
        raise OSError("synthetic explanation provider outage")
    monkeypatch.setattr(review_explanation, "invoke_for_purpose", unavailable)
    with demo_session() as session:
        yield session


def held_package(session):
    state = autonomy_service.get(session, "demo-agent", "create_file")
    state.mode = "ASSISTED"
    session.add(state)
    session.flush()
    held = propose(session, "create_file", parameters={"content": "Hello"})
    assert held.decision == "PENDING_APPROVAL"
    session.commit()
    package = next(p for p in escalation_service.list_pending(session)
                   if p.decision_id == held.decision_id)
    payload = {"decision_id": held.decision_id, "reviewer_id": "test-reviewer",
               "approve": True, "note": "Reviewed", "package_fingerprint": package.package_fingerprint}
    session.rollback()
    return held, package, payload


def durable_state(engine):
    # Operational monitoring is intentionally outside the authorization transaction.
    with Session(engine) as session:
        return {
            "decisions": [r.model_dump(mode="json") for r in session.exec(
                select(DecisionRecord).order_by(DecisionRecord.decision_id)).all()],
            "reviews": [r.model_dump(mode="json") for r in session.exec(select(HumanReviewRecord)).all()],
            "queue": [r.model_dump(mode="json") for r in session.exec(select(EscalationQueueItem)).all()],
        }


@pytest.mark.parametrize("note", [None, "", "  \t\n", "__omit__"])
def test_invalid_approval_note_leaves_review_pending(review_session, note):
    held, _, payload = held_package(review_session)
    if note == "__omit__":
        payload.pop("note")
    else:
        payload["note"] = note
    before = durable_state(review_session.bind)
    with TestClient(create_app(review_session.bind)) as client:
        response = client.post("/api/v1/control/approve/" + held.decision_id, json=payload)
    assert response.status_code == 422
    assert durable_state(review_session.bind) == before
    with pytest.raises(ValidationError):
        ApprovalDecision.model_validate(payload)


def test_wrong_fingerprint_is_conflict_without_mutation(review_session):
    held, _, payload = held_package(review_session)
    payload["package_fingerprint"] = "0" * 64
    before = durable_state(review_session.bind)
    with TestClient(create_app(review_session.bind)) as client:
        response = client.post("/api/v1/control/approve/" + held.decision_id, json=payload)
    assert response.status_code == 409
    assert "fingerprint" in response.json()["detail"]
    assert durable_state(review_session.bind) == before


def test_policy_change_blocks_approval_but_allows_rejection(review_session):
    held, _, payload = held_package(review_session)
    candidate = review_session.exec(select(Policy).where(Policy.is_shadow == True)).one()
    activate(review_session, candidate, "fixture-policy-reviewer")
    review_session.commit()
    before = durable_state(review_session.bind)
    with TestClient(create_app(review_session.bind)) as client:
        response = client.post("/api/v1/control/approve/" + held.decision_id, json=payload)
        assert response.status_code == 409
        assert "active policy changed" in response.json()["detail"]
        assert durable_state(review_session.bind) == before
        payload.update(approve=False, note=None)
        rejected = client.post("/api/v1/control/approve/" + held.decision_id, json=payload)
        assert rejected.status_code == 200
        assert rejected.json()["authorization_status"] == "NOT_REQUESTED"


def test_approval_and_replay_make_no_cloud_calls(review_session, monkeypatch):
    held, package, payload = held_package(review_session)
    from src.services import (advisory_evidence, embeddings, llm_parser,
                              request_analysis, semantic_judge, semantic_retrieval)
    def forbidden(*args, **kwargs):
        pytest.fail("approval/replay unexpectedly invoked a cloud analysis stage")
    for module, name in [(advisory_evidence, "build_evidence"), (llm_parser, "parse"),
                         (request_analysis, "analyze"), (semantic_judge, "judge"),
                         (semantic_retrieval, "retrieve"), (review_explanation, "invoke_for_purpose")]:
        monkeypatch.setattr(module, name, forbidden)
    # Block the embedding API at its existing public entry point too.
    for name in ("query_embedding", "gemini_embed", "jina_embed", "aicredits_embed"):
        monkeypatch.setattr(embeddings, name, forbidden)
    before = durable_state(review_session.bind)
    with TestClient(create_app(review_session.bind)) as client:
        listed = client.get("/api/v1/escalations").json()
        assert next(p for p in listed if p["decision_id"] == held.decision_id)["package_fingerprint"] == package.package_fingerprint
        response = client.post("/api/v1/control/approve/" + held.decision_id, json=payload)
        assert response.status_code == 200
        result = response.json()
        assert result["authorization_status"] == "ALLOWED"
        assert result["new_policy_version"] is None
        assert client.get("/api/v1/replay/" + result["authorization_decision_id"]).json()["is_consistent"]
    after = durable_state(review_session.bind)
    assert next(r for r in after["decisions"] if r["decision_id"] == held.decision_id) == next(
        r for r in before["decisions"] if r["decision_id"] == held.decision_id)
    review = after["reviews"][0]
    assert review["review_id"] == result["review_id"]
    assert review["authorization_decision_id"] == result["authorization_decision_id"]
    assert review["note"] == payload["note"]


def test_failed_approval_commit_rolls_back_review_and_authorization(review_session):
    held, _, payload = held_package(review_session)
    before = durable_state(review_session.bind)
    # The new review can leave the identity map after flush, so inspect the
    # transaction's durable pending insert rather than relying on weak references.
    def fail_commit(session):
        if session.exec(select(HumanReviewRecord)).first() is not None:
            raise RuntimeError("synthetic finalization commit failure")
    with TestClient(create_app(review_session.bind)) as client:
        event.listen(Session, "before_commit", fail_commit)
        try:
            response = client.post("/api/v1/control/approve/" + held.decision_id, json=payload)
        finally:
            event.remove(Session, "before_commit", fail_commit)
    assert response.status_code == 503
    assert "capability_token" not in response.text
    assert durable_state(review_session.bind) == before


def test_explanation_fallback_is_complete_and_package_reads_are_stable(review_session):
    held, package, _ = held_package(review_session)
    assert package.explanation.provenance["source"] == "deterministic_fallback"
    assert package.explanation.provenance["failures"]
    assert {r.requirement_id for r in package.review_requirements} == {
        e.requirement_id for e in package.explanation.explanations}
    assert "review_assisted_signoff" in {r.requirement_id for r in package.review_requirements}
    again = next(p for p in escalation_service.list_pending(review_session)
                 if p.decision_id == held.decision_id)
    assert again.model_dump() == package.model_dump()


def test_requirement_builder_covers_early_gates_gaps_and_unknown_dispositions(review_session):
    held, package, _ = held_package(review_session)
    original = review_session.get(DecisionRecord, held.decision_id)
    evidence = deepcopy(original.evidence_json)
    policy = review_session.exec(select(Policy).where(Policy.policy_hash == held.policy_hash)).one()
    # This focused builder fixture is intentionally not a full pipeline envelope.
    # Full envelope validation is performed by the semantic pipeline's own tests.
    package = package.model_copy(update={"original_outcome": "ESCALATED", "semantic_findings": {
        "analysis": {"status": "applied", "result": {"unknowns": ["resolved detail", "irrelevant detail", "open question"]}},
        "judgment": {"status": "applied", "result": {"findings": [], "unknown_dispositions": [
            {"unknown_index": 0, "disposition": "resolved"},
            {"unknown_index": 1, "disposition": "irrelevant"},
            {"unknown_index": 2, "disposition": "unresolved"}]}},
        "summary": {"reasons": [{"code": "candidate_budget", "stage": "retrieval",
                                  "message": "Candidate budget omitted retrieved evidence."}]},
    }})
    requirements = review_explanation.build_requirements(package, evidence, policy.rules_json)
    by_id = {r.requirement_id: r for r in requirements}
    assert "review_assisted_signoff" in by_id
    assert by_id["review_unresolved_semantic_details"].unknowns == ["open question"]
    assert by_id["review_semantic_summary_0"].reason == "Candidate budget omitted retrieved evidence."
    environment_package = package.model_copy(update={"request": package.request.model_copy(
        update={"action_class": "delete_file", "environment": "production"})})
    requirements = review_explanation.build_requirements(environment_package, evidence, policy.rules_json)
    assert "review_environment" in {r.requirement_id for r in requirements}


def test_legacy_pending_package_requires_explicit_preparation(review_session):
    held, package, payload = held_package(review_session)
    row = review_session.exec(select(EscalationQueueItem).where(
        EscalationQueueItem.decision_id == held.decision_id)).one()
    legacy = package.model_dump(mode="json")
    for key in ("package_version", "original_outcome", "original_policy_hash", "action_fingerprint",
                "review_requirements", "explanation", "package_fingerprint"):
        legacy.pop(key)
    row.package_json = legacy
    review_session.add(row)
    review_session.commit()
    with TestClient(create_app(review_session.bind)) as client:
        assert client.post("/api/v1/control/approve/" + held.decision_id, json=payload).status_code == 409
        prepared = client.post("/api/v1/escalations/" + held.decision_id + "/prepare")
        assert prepared.status_code == 200
        fresh = next(p for p in client.get("/api/v1/escalations").json() if p["decision_id"] == held.decision_id)
        assert fresh["package_fingerprint"] == prepared.json()["package_fingerprint"]
        payload.update(approve=False, note=None, package_fingerprint=fresh["package_fingerprint"])
        assert client.post("/api/v1/control/approve/" + held.decision_id, json=payload).status_code == 200


def test_shadow_approval_is_recorded_but_cannot_execute(review_session):
    held = propose(review_session, "delete_file", role="admin_agent", environment="production")
    assert held.decision == "ESCALATED"
    package = next(p for p in escalation_service.list_pending(review_session) if p.decision_id == held.decision_id)
    review_session.commit()
    with TestClient(create_app(review_session.bind)) as client:
        response = client.post("/api/v1/control/approve/" + held.decision_id, json={
            "decision_id": held.decision_id, "reviewer_id": "test-reviewer", "approve": True,
            "note": "Reviewed", "package_fingerprint": package.package_fingerprint})
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "APPROVED" and data["authorization_status"] == "BLOCKED"
        assert data["evaluation"]["decision"] == "SHADOW_LOGGED"
        assert data["capability_token"] is None


@pytest.mark.parametrize("bad_output", ["missing_requirement", "unknown_reference"])
def test_invalid_model_explanation_uses_complete_fallback(review_session, monkeypatch, bad_output):
    _, package, _ = held_package(review_session)
    requirements = package.review_requirements
    output = review_explanation._fallback(requirements, package.request).model_dump()
    if bad_output == "missing_requirement":
        output["explanations"].pop()
    else:
        output["explanations"][0]["evidence_refs"] = ["invented.evidence"]
    monkeypatch.setattr(review_explanation, "invoke_for_purpose", lambda *args, **kwargs: (
        ReviewExplanationOutput.model_validate(output), SimpleNamespace(provider="fixture", model="fixture")))
    original = review_session.get(DecisionRecord, package.decision_id)
    policy = review_session.exec(select(Policy).where(Policy.policy_hash == original.policy_hash)).one()
    expected, explanation = review_explanation.explain(package, original.evidence_json, policy.rules_json)
    assert explanation.provenance["source"] == "deterministic_fallback"
    assert explanation.provenance["provider"] is None
    assert explanation.provenance["failures"]
    assert {r.requirement_id for r in expected} == {e.requirement_id for e in explanation.explanations}
