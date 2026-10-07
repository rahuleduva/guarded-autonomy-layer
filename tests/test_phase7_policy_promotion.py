from sqlmodel import select
from src.config import settings
from src.evaluation import propose
from src.models.policy import Policy
from src.models.runtime import PolicyPromotionEvent
from src.services import policy_promotion


def test_candidate_needs_evidence_and_explicit_review(runtime_session):
    session = runtime_session
    candidate = session.exec(select(Policy).where(Policy.is_shadow == True)).one()
    old = session.exec(select(Policy).where(Policy.is_active == True)).one()
    assert not policy_promotion.promote_candidate(session, candidate.policy_hash, "reviewer", True)
    for _ in range(settings.PROMOTION_THRESHOLD_POLICY):
        propose(session)
    assert policy_promotion.check_and_promote(session, candidate.policy_hash)
    assert old.is_active and candidate.is_shadow
    assert not policy_promotion.promote_candidate(session, candidate.policy_hash, "reviewer", False)
    assert policy_promotion.promote_candidate(session, candidate.policy_hash, "reviewer", True)
    assert candidate.is_active and not candidate.is_shadow and candidate.reviewer_id == "reviewer"
    assert not old.is_active and old.activated_at is None and old.deactivated_at
    events = session.exec(select(PolicyPromotionEvent)).all()
    assert [event.promoted for event in events] == [False, False, True]
    assert len(events[-1].evidence_json["shadow_ids"]) == settings.PROMOTION_THRESHOLD_POLICY


def test_candidate_with_violation_cannot_promote(runtime_session):
    candidate = runtime_session.exec(select(Policy).where(Policy.is_shadow == True)).one()
    propose(runtime_session, target_resource="/outside")
    for _ in range(settings.PROMOTION_THRESHOLD_POLICY):
        propose(runtime_session)
    assert not policy_promotion.check_and_promote(runtime_session, candidate.policy_hash)
    assert policy_promotion.evidence(runtime_session, candidate.policy_hash)["violations"]
