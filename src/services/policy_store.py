"""Transactional movement of the active policy pointer."""
from datetime import datetime, timezone
from sqlmodel import Session, select
from src.models.policy import Policy
from src.services.policy_loader import canonical_policy_hash, validate_policy


def validate_stored(policy: Policy) -> None:
    validate_policy(policy.rules_json)
    if policy.version != policy.rules_json["version"] or canonical_policy_hash(policy.rules_json) != policy.policy_hash:
        raise ValueError("stored policy version or content hash does not match")


def active(session: Session) -> Policy:
    row = session.exec(select(Policy).where(Policy.is_active == True).with_for_update()).one_or_none()
    if row is None:
        raise ValueError("no active policy")
    return row


def activate(session: Session, candidate: Policy, reviewer_id: str) -> None:
    validate_stored(candidate)
    old = active(session)
    if old.policy_hash == candidate.policy_hash:
        raise ValueError("policy is already active")
    now = datetime.now(timezone.utc)
    old.is_active, old.activated_at, old.deactivated_at = False, None, now
    session.add(old)
    session.flush()  # release partial unique index before acquiring it
    candidate.is_active, candidate.is_shadow = True, False
    candidate.activated_at, candidate.deactivated_at = now, None
    candidate.reviewer_id = reviewer_id
    session.add(candidate)
    session.flush()


def reviewer_bump(session: Session, reviewer_id: str) -> Policy:
    import copy
    current = active(session)
    validate_stored(current)
    rules = copy.deepcopy(current.rules_json)
    try:
        major, minor, patch = (int(part) for part in current.version.split("."))
    except (ValueError, TypeError):
        raise ValueError("reviewer policy bumps require major.minor.patch versions") from None
    versions = set(session.exec(select(Policy.version)).all())
    version = f"{major}.{minor}.{patch + 1}"
    while version in versions:
        patch += 1
        version = f"{major}.{minor}.{patch + 1}"
    rules["version"] = version
    row = Policy(version=version, policy_hash=canonical_policy_hash(rules), rules_json=rules)
    session.add(row)
    session.flush()
    activate(session, row, reviewer_id)
    return row
