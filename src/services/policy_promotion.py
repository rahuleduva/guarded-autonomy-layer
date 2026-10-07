"""Evidence qualifies a candidate; only a reviewer moves the active pointer."""
from sqlmodel import Session, select
from src.config import settings
from src.models.decision import DecisionRecord
from src.models.policy import Policy
from src.models.policy_shadow import ShadowRecord
from src.models.runtime import PolicyPromotionEvent
from src.services.policy_store import activate

_CLEAN = {"SHADOW_LOGGED", "PENDING_APPROVAL", "ALLOWED"}


def evidence(session: Session, candidate_policy_hash: str) -> dict:
    rows = session.exec(select(ShadowRecord, DecisionRecord).join(
        DecisionRecord, DecisionRecord.decision_id == ShadowRecord.decision_id
    ).where(ShadowRecord.candidate_policy_hash == candidate_policy_hash)).all()
    violations = [shadow.shadow_id for shadow, active in rows
                  if shadow.would_have_decided not in _CLEAN or active.outcome not in _CLEAN]
    threshold = settings.PROMOTION_THRESHOLD_POLICY
    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold <= 0:
        raise ValueError("policy promotion threshold must be positive")
    return {"runs": len(rows), "threshold": threshold, "violations": violations,
            "shadow_ids": [shadow.shadow_id for shadow, _ in rows],
            "qualified": len(rows) >= threshold and not violations}


def check_and_promote(session: Session, candidate_policy_hash: str) -> bool:
    candidate = session.exec(select(Policy).where(Policy.policy_hash == candidate_policy_hash)).one_or_none()
    return bool(candidate and candidate.is_shadow and not candidate.is_active
                and evidence(session, candidate_policy_hash)["qualified"])


def promote_candidate(session: Session, candidate_policy_hash: str,
                      reviewer_id: str, approved: bool) -> bool:
    if not reviewer_id.strip() or not isinstance(approved, bool):
        raise ValueError("reviewer ID and boolean approved are required")
    candidate = session.exec(select(Policy).where(
        Policy.policy_hash == candidate_policy_hash).with_for_update()).one_or_none()
    if candidate is None:
        raise ValueError("candidate policy not found")
    frozen = evidence(session, candidate_policy_hash)
    promoted = approved and candidate.is_shadow and not candidate.is_active and frozen["qualified"]
    session.add(PolicyPromotionEvent(policy_hash=candidate_policy_hash, reviewer_id=reviewer_id,
                                    approved=approved, promoted=promoted, evidence_json=frozen))
    if promoted:
        activate(session, candidate, reviewer_id)
    session.flush()
    return bool(promoted)
