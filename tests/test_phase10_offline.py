from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, select
from src.config import settings, Settings
from src.evaluation import propose, earn_live
from src.main import create_app
from src.models.decision import DecisionRecord
from src.models.request import NaturalLanguageRequest
from src.models.runtime import SimulatedResource
from src.services import llm_parser, semantic_engine


def test_offline_prompt_pipeline_with_no_keys(runtime_session, monkeypatch):
    for field in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY", "QDRANT_API_KEY", "QDRANT_URL"):
        monkeypatch.setattr(settings, field, "")
    monkeypatch.setattr(llm_parser, "structured_parser", lambda _: pytest.fail("offline called an LLM"))
    monkeypatch.setattr(semantic_engine, "_query_qdrant", lambda _: pytest.fail("offline called Qdrant"))
    request = llm_parser.parse(NaturalLanguageRequest(agent_id="demo-agent", actor_role="workspace_agent",
        raw_prompt="create file /srv/agent/workspace/demo.txt with content hello in development"))
    from src.services.orchestrator import evaluate_action
    result = evaluate_action(runtime_session, request)
    assert result.decision == "SHADOW_LOGGED" and result.capability_token is None
    assert request.parameters["content"] == "hello"
    assert Settings(_env_file=None, OFFLINE_MODE=True).DATABASE_URL.startswith("sqlite:")


def test_online_parser_failure_falls_back_and_preserves_identity(monkeypatch):
    monkeypatch.setattr(settings, "OFFLINE_MODE", False)
    def unavailable(_):
        raise TimeoutError("provider unavailable")
    monkeypatch.setattr(llm_parser, "structured_parser", unavailable)
    parsed = llm_parser.parse(NaturalLanguageRequest(agent_id="caller", actor_role="workspace_agent",
        raw_prompt="read file notes.txt"))
    assert parsed.agent_id == "caller" and parsed.actor_role == "workspace_agent"
    assert parsed.environment == "production"
    assert parsed.parameters["parser_provenance"]["fallback_reason"] == "TimeoutError"
    with pytest.raises(ValueError):
        llm_parser.parse(NaturalLanguageRequest(agent_id="caller", actor_role="workspace_agent", raw_prompt="do something useful"))


def test_provider_structured_output_cannot_invent_identity(monkeypatch):
    monkeypatch.setattr(settings, "OFFLINE_MODE", False)
    fake = SimpleNamespace(invoke=lambda _: {"action_class": "read_file", "target_resource": "note.txt", "agent_id": "attacker"})
    monkeypatch.setattr(llm_parser, "structured_parser", lambda _: fake)
    parsed = llm_parser.parse(NaturalLanguageRequest(agent_id="caller", actor_role="workspace_agent", raw_prompt="read file note.txt"))
    assert parsed.agent_id == "caller"
    assert parsed.parameters["parser_provenance"]["source"] == "offline-grammar-v1"


def test_api_evaluate_execute_replay_and_reject_duplicate(runtime_session):
    session = runtime_session
    earn_live(session)
    session.commit()
    with TestClient(create_app(session.bind)) as client:
        body = {"agent_id": "demo-agent", "actor_role": "workspace_agent", "action_class": "create_file",
                "target_resource": "/srv/agent/workspace/api.txt", "environment": "development"}
        result = client.post("/api/v1/actions/evaluate", json=body)
        assert result.status_code == 200
        approved = result.json()
        assert approved["decision"] == "ALLOWED" and approved["capability_token"]
        payload = {"decision_id": approved["decision_id"], "token": approved["capability_token"]}
        assert client.post("/api/v1/actions/execute", json=payload).json()["status"] == "EXECUTED"
        assert client.post("/api/v1/actions/execute", json=payload).json()["status"] == "REJECTED"
        assert client.get(f"/api/v1/replay/{approved['decision_id']}").json()["is_consistent"]
        body["request_id"] = approved["request_id"]
        assert client.post("/api/v1/actions/evaluate", json=body).status_code == 409
        assert client.get("/api/v1/replay/missing").status_code == 404


def test_api_failed_commit_releases_no_authorization(runtime_session):
    session = runtime_session
    earn_live(session)
    session.commit()
    with TestClient(create_app(session.bind)) as client:
        def fail_commit(_session):
            raise RuntimeError("simulated failed audit commit")
        event.listen(Session, "before_commit", fail_commit)
        try:
            result = client.post("/api/v1/actions/evaluate", json={"agent_id": "demo-agent", "actor_role": "workspace_agent",
                "action_class": "create_file", "target_resource": "/srv/agent/workspace/fail.txt", "environment": "development"})
        finally:
            event.remove(Session, "before_commit", fail_commit)
        assert result.status_code == 503
        assert "capability_token" not in result.text
    session.expire_all()
    assert not any(row.request_json["target_resource"].endswith("fail.txt") for row in session.exec(select(DecisionRecord)).all())
    assert session.exec(select(SimulatedResource)).all() == []


def test_api_human_review_and_policy_promotion(runtime_session):
    session = runtime_session
    for _ in range(settings.PROMOTION_THRESHOLD_ASSISTED):
        propose(session, "create_file")
    session.commit()
    with TestClient(create_app(session.bind)) as client:
        held = client.post("/api/v1/actions/evaluate-prompt", json={"agent_id": "demo-agent", "actor_role": "workspace_agent",
            "raw_prompt": "create file /srv/agent/workspace/review.txt in development"}).json()
        assert held["decision"] == "PENDING_APPROVAL"
        packages = client.get("/api/v1/escalations").json()
        assert any(package["decision_id"] == held["decision_id"] for package in packages)
        review = {"decision_id": held["decision_id"], "reviewer_id": "api-reviewer", "approve": True}
        assert client.post("/api/v1/control/approve/wrong-id", json=review).status_code == 400
        approved = client.post(f"/api/v1/control/approve/{held['decision_id']}", json=review)
        assert approved.status_code == 200 and approved.json()["new_policy_version"] == "1.0.1"
        assert approved.json()["evaluation"]["capability_token"]
        assert client.post(f"/api/v1/control/approve/{held['decision_id']}", json=review).status_code == 400
        assert client.post("/api/v1/control/promote-agent", json={"agent_id": "new", "action_class": "read_file",
            "reviewer_id": "api-reviewer", "approve": True}).json()["promoted"] is False
        from src.models.policy import Policy
        candidate = session.exec(select(Policy).where(Policy.is_shadow == True)).one()
        for _ in range(settings.PROMOTION_THRESHOLD_POLICY):
            propose(session)
        session.commit()
        response = client.post("/api/v1/control/promote-policy", json={"candidate_policy_hash": candidate.policy_hash,
            "reviewer_id": "api-reviewer", "approve": True})
        assert response.status_code == 200 and response.json()["promoted"] is True
        assert response.json()["active_policy_hash"] == candidate.policy_hash


def test_failed_execution_commit_rolls_back_resources_and_nonce(runtime_session):
    session = runtime_session
    earn_live(session)
    allowed = propose(session, "create_file")
    session.commit()
    with TestClient(create_app(session.bind)) as client:
        payload = {"decision_id": allowed.decision_id, "token": allowed.capability_token}
        def fail_commit(_session):
            raise RuntimeError("simulated failed execution commit")
        event.listen(Session, "before_commit", fail_commit)
        try:
            assert client.post("/api/v1/actions/execute", json=payload).status_code == 503
        finally:
            event.remove(Session, "before_commit", fail_commit)
        # A rolled-back nonce remains usable; the resource was not committed.
        assert client.post("/api/v1/actions/execute", json=payload).json()["status"] == "EXECUTED"
        assert client.post("/api/v1/actions/execute", json=payload).json()["status"] == "REJECTED"
