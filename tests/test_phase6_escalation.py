import pytest
from sqlmodel import select
from src.config import settings
from src.evaluation import propose
from src.models.decision import DecisionRecord
from src.models.policy import Policy
from src.services import escalation_service, replay_engine
from src.services.policy_engine import evaluate


def assisted(session):
    for _ in range(settings.PROMOTION_THRESHOLD_ASSISTED):
        propose(session, "create_file")
    return propose(session, "create_file")


def test_approval_bumps_policy_binds_action_and_preserves_original(runtime_session):
    session = runtime_session
    held = assisted(session)
    original = session.get(DecisionRecord, held.decision_id)
    before = original.evidence_json.copy()
    result = escalation_service.decide(session, held.decision_id, "reviewer", True, "approved once")
    approved = result["evaluation"]
    assert approved["decision"] == "ALLOWED" and approved["capability_token"]
    assert result["new_policy_version"] == "1.0.1"
    assert approved["policy_hash"] != held.policy_hash
    assert original.outcome == "PENDING_APPROVAL" and original.evidence_json == before
    assert replay_engine.replay(session, held.decision_id).is_consistent
    assert replay_engine.replay(session, approved["decision_id"]).is_consistent
    assert not escalation_service.list_pending(session)
    with pytest.raises(ValueError, match="already reviewed"):
        escalation_service.decide(session, held.decision_id, "reviewer", True)
    fresh = propose(session, "create_file")
    assert fresh.decision == "PENDING_APPROVAL" and fresh.capability_token is None
    row = session.get(DecisionRecord, approved["decision_id"])
    policy = session.exec(select(Policy).where(Policy.policy_hash == row.policy_hash)).one()
    from src.models.request import ActionRequest
    from src.models.autonomy import AutonomyState
    changed = ActionRequest.model_validate(row.request_json).model_copy(update={"target_resource": "/srv/agent/workspace/other.txt"})
    assert evaluate(changed, policy, row.evidence_json, AutonomyState(**row.evidence_json["autonomy_snapshot"])).outcome != "ALLOWED"


def test_rejection_does_not_bump_or_issue_token(runtime_session):
    held = assisted(runtime_session)
    before = runtime_session.exec(select(Policy)).all()
    result = escalation_service.decide(runtime_session, held.decision_id, "reviewer", False)
    assert result == {"status": "REJECTED", "new_policy_version": None, "evaluation": None}
    assert len(runtime_session.exec(select(Policy)).all()) == len(before)


def test_critical_review_stays_held_and_outside_scope_never_queued(runtime_session):
    denied = propose(runtime_session, target_resource="/etc/passwd")
    with pytest.raises(ValueError, match="only held"):
        escalation_service.raise_escalation(runtime_session, denied.decision_id)
    held = propose(runtime_session, "delete_directory", role="admin_agent")
    assert held.decision == "ESCALATED"
    review = escalation_service.decide(runtime_session, held.decision_id, "reviewer", True)
    assert review["evaluation"]["decision"] == "ESCALATED"
    assert review["evaluation"]["capability_token"] is None
