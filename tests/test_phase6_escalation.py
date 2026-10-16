import pytest
from sqlmodel import select
from src.config import settings
from src.evaluation import propose
from src.models.autonomy import AutonomyState
from src.models.decision import DecisionRecord
from src.models.policy import Policy
from src.models.request import ActionRequest, ApprovalDecision
from src.services import escalation_service, replay_engine
from src.services.policy_engine import evaluate


def assisted(session):
    for _ in range(settings.PROMOTION_THRESHOLD_ASSISTED):
        propose(session, "create_file")
    return propose(session, "create_file")


def review_payload(session, held, *, approve=True, note="approved once"):
    package = next(item for item in escalation_service.list_pending(session)
                   if item.decision_id == held.decision_id)
    return ApprovalDecision(
        decision_id=held.decision_id, reviewer_id="reviewer", approve=approve,
        note=note, package_fingerprint=package.package_fingerprint,
    )


def test_approval_finalizes_without_policy_bump_and_preserves_original(runtime_session):
    session = runtime_session
    held = assisted(session)
    original = session.get(DecisionRecord, held.decision_id)
    before = original.evidence_json.copy()
    policies_before = [(row.policy_hash, row.version, row.is_active)
                       for row in session.exec(select(Policy).order_by(Policy.policy_hash)).all()]
    submission = review_payload(session, held)
    result = escalation_service.decide(session, held.decision_id, submission)
    approved = result.evaluation
    assert result.authorization_status == "ALLOWED"
    assert approved["decision"] == "ALLOWED" and approved["capability_token"]
    assert result.new_policy_version is None
    assert approved["policy_hash"] == held.policy_hash
    assert original.outcome == "PENDING_APPROVAL" and original.evidence_json == before
    assert original.reviewer_id is None
    assert replay_engine.replay(session, held.decision_id).is_consistent
    assert replay_engine.replay(session, approved["decision_id"]).is_consistent
    assert not escalation_service.list_pending(session)
    policies_after = [(row.policy_hash, row.version, row.is_active)
                      for row in session.exec(select(Policy).order_by(Policy.policy_hash)).all()]
    assert policies_after == policies_before
    with pytest.raises(escalation_service.ReviewConflictError, match="already reviewed"):
        escalation_service.decide(session, held.decision_id, submission)
    fresh = propose(session, "create_file")
    assert fresh.decision == "PENDING_APPROVAL" and fresh.capability_token is None
    row = session.get(DecisionRecord, approved["decision_id"])
    policy = session.exec(select(Policy).where(Policy.policy_hash == row.policy_hash)).one()
    changed = ActionRequest.model_validate(row.request_json).model_copy(
        update={"target_resource": "/srv/agent/workspace/other.txt"})
    assert evaluate(changed, policy, row.evidence_json,
                    AutonomyState(**row.evidence_json["autonomy_snapshot"])).outcome != "ALLOWED"


def test_rejection_records_feedback_without_authorization(runtime_session):
    held = assisted(runtime_session)
    before = [(row.policy_hash, row.version, row.is_active)
              for row in runtime_session.exec(select(Policy).order_by(Policy.policy_hash)).all()]
    result = escalation_service.decide(
        runtime_session, held.decision_id,
        review_payload(runtime_session, held, approve=False, note=None),
    )
    assert result.status == "REJECTED"
    assert result.authorization_status == "NOT_REQUESTED"
    assert result.evaluation is None and result.capability_token is None
    after = [(row.policy_hash, row.version, row.is_active)
             for row in runtime_session.exec(select(Policy).order_by(Policy.policy_hash)).all()]
    assert after == before


def test_hard_checks_still_block_approved_critical_action(runtime_session):
    denied = propose(runtime_session, target_resource="/etc/passwd")
    with pytest.raises(ValueError, match="only held"):
        escalation_service.raise_escalation(runtime_session, denied.decision_id)
    held = propose(runtime_session, "delete_directory", role="admin_agent")
    assert held.decision == "ESCALATED"
    review = escalation_service.decide(
        runtime_session, held.decision_id, review_payload(runtime_session, held))
    assert review.status == "APPROVED"
    assert review.authorization_status == "BLOCKED"
    assert review.evaluation["decision"] == "ESCALATED"
    assert review.capability_token is None
