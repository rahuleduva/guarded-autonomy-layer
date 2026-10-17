"""Transactional movement of the active policy pointer."""
from datetime import datetime, timezone
from hashlib import sha256
from sqlalchemy import text
from sqlmodel import Session, select
from src.models.policy import Policy
from src.services.policy_loader import canonical_policy_hash, validate_policy


# This identity stays the same when the active policy row changes.
_ACTIVE_POLICY_LOCK_KEY = int.from_bytes(
    sha256(b'guarded-autonomy-layer:active-policy').digest()[:8], 'big', signed=True,
)


def lock_active_policy(session: Session) -> None:
    """Coordinate active-policy readers and writers until commit or rollback."""
    if session.get_bind().dialect.name == 'postgresql':
        # Acquire in a separate statement: the following policy query gets a
        # fresh READ COMMITTED snapshot after any concurrent promotion commits.
        session.execute(text('SELECT pg_advisory_xact_lock(:lock_key)'),
                        {'lock_key': _ACTIVE_POLICY_LOCK_KEY}).close()


def validate_stored(policy: Policy) -> None:
    validate_policy(policy.rules_json)
    if policy.version != policy.rules_json["version"] or canonical_policy_hash(policy.rules_json) != policy.policy_hash:
        raise ValueError("stored policy version or content hash does not match")


def active(session: Session) -> Policy:
    lock_active_policy(session)
    row = session.exec(select(Policy).where(Policy.is_active == True)
                       .with_for_update().execution_options(populate_existing=True)).one_or_none()
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
