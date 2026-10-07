"""Persisted, ledger-backed autonomy and attributed promotion (Phase 5)."""

from datetime import datetime, timezone
from itertools import groupby
from typing import Optional

from sqlmodel import Session, select

from src.config import settings
from src.models.autonomy import AutonomyPromotionEvent, AutonomyState
from src.models.decision import DecisionRecord
from src.models.enums import AutonomyLevel, DecisionOutcome

_CLEAN_OUTCOMES = {
    DecisionOutcome.SHADOW_LOGGED.value,
    DecisionOutcome.PENDING_APPROVAL.value,
    DecisionOutcome.ALLOWED.value,
}


def get(session: Session, agent_id: str, action_class: str) -> AutonomyState:
    """Get or create SHADOW state and lock it for this caller-owned transaction."""
    if not agent_id.strip() or not action_class.strip():
        raise ValueError("agent_id and action_class must be non-empty")
    state = session.exec(
        select(AutonomyState).where(
            AutonomyState.agent_id == agent_id,
            AutonomyState.action_class == action_class,
        ).with_for_update()
    ).one_or_none()
    if state is None:
        state = AutonomyState(agent_id=agent_id, action_class=action_class,
                              mode=AutonomyLevel.SHADOW.value, streak=0)
        session.add(state)
        session.flush()
    return state


def _clean_runs(session: Session, agent_id: str, action_class: str) -> list[DecisionRecord]:
    records = session.exec(
        select(DecisionRecord).where(
            DecisionRecord.agent_id == agent_id,
            DecisionRecord.action_class == action_class,
        ).order_by(DecisionRecord.created_at.desc(), DecisionRecord.decision_id.desc())
    ).all()
    clean = []
    # If timestamps tie, a violation takes precedence over clean decisions in
    # that group. UUID order must not manufacture evidence of a later clean run.
    for _, group in groupby(records, key=lambda row: row.created_at):
        batch = list(group)
        if any(row.outcome not in _CLEAN_OUTCOMES for row in batch):
            break
        clean.extend(batch)
    return clean


def _threshold(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("promotion threshold must be a positive integer")
    return value


def record_outcome(
    session: Session, agent_id: str, action_class: str,
    violated: Optional[bool] = None,
) -> AutonomyState:
    """Refresh the streak from ledger evidence; repeated calls cannot inflate it.

    The optional caller assertion is checked against the newest ledger decision.
    It cannot manufacture a clean run. No decision means no outcome to record.
    DENIED and ESCALATED reset the streak; policy-permitted outcomes count as
    clean proposals, not proof of executed actions or human approval.
    """
    state = get(session, agent_id, action_class)
    latest = session.exec(
        select(DecisionRecord).where(
            DecisionRecord.agent_id == agent_id,
            DecisionRecord.action_class == action_class,
        ).order_by(DecisionRecord.created_at.desc(), DecisionRecord.decision_id.desc())
    ).first()
    if latest is None:
        raise ValueError("cannot record an outcome without a ledger decision")
    if violated is not None:
        if not isinstance(violated, bool) or violated != (latest.outcome not in _CLEAN_OUTCOMES):
            raise ValueError("violated assertion does not match the ledger decision")
    state.streak = len(_clean_runs(session, agent_id, action_class))
    state.updated_at = datetime.now(timezone.utc)
    session.add(state)
    session.flush()
    maybe_auto_promote(session, agent_id, action_class)
    return state


def _log_promotion(
    session: Session, state: AutonomyState, requested_mode: AutonomyLevel,
    reviewer_id: Optional[str], approved: bool, promoted: bool, reason: str,
    runs: list[DecisionRecord], threshold: int,
) -> None:
    session.add(AutonomyPromotionEvent(
        autonomy_state_id=state.id,
        previous_mode=state.mode,
        requested_mode=requested_mode.value,
        reviewer_id=reviewer_id,
        approved=approved,
        promoted=promoted,
        reason=reason,
        evidence_json={
            "streak": len(runs), "threshold": threshold,
            "decision_ids": [row.decision_id for row in runs],
        },
    ))


def maybe_auto_promote(session: Session, agent_id: str, action_class: str) -> bool:
    """Automatically grant SHADOW -> ASSISTED from clean recorded shadow runs."""
    threshold = _threshold(settings.PROMOTION_THRESHOLD_ASSISTED)
    state = get(session, agent_id, action_class)
    runs = _clean_runs(session, agent_id, action_class)
    state.streak = len(runs)
    if state.mode != AutonomyLevel.SHADOW.value or len(runs) < threshold:
        session.add(state)
        session.flush()
        return False
    if any(row.outcome != DecisionOutcome.SHADOW_LOGGED.value
           or row.autonomy_level != AutonomyLevel.SHADOW.value for row in runs[:threshold]):
        return False

    _log_promotion(session, state, AutonomyLevel.ASSISTED, None, True, True,
                   "clean shadow evidence met the threshold", runs, threshold)
    state.mode = AutonomyLevel.ASSISTED.value
    state.promoted_at = datetime.now(timezone.utc)
    state.updated_at = state.promoted_at
    session.add(state)
    session.flush()
    return True


def promote_to_live(
    session: Session, agent_id: str, action_class: str,
    reviewer_id: str, approved: bool,
) -> bool:
    """Record approve/decline and grant LIVE only from qualified ASSISTED state."""
    if not reviewer_id.strip() or not isinstance(approved, bool):
        raise ValueError("reviewer_id and a boolean approved decision are required")
    threshold = _threshold(settings.PROMOTION_THRESHOLD_LIVE)
    state = get(session, agent_id, action_class)
    runs = _clean_runs(session, agent_id, action_class)
    state.streak = len(runs)
    promoted = approved and state.mode == AutonomyLevel.ASSISTED.value and len(runs) >= threshold
    if not approved:
        reason = "reviewer declined LIVE promotion"
    elif state.mode != AutonomyLevel.ASSISTED.value:
        reason = "LIVE promotion requires ASSISTED state"
    elif len(runs) < threshold:
        reason = "insufficient clean ledger evidence"
    else:
        reason = "reviewer approved qualified LIVE promotion"
    _log_promotion(session, state, AutonomyLevel.LIVE, reviewer_id, approved,
                   promoted, reason, runs, threshold)
    if promoted:
        state.mode = AutonomyLevel.LIVE.value
        state.reviewer_id = reviewer_id
        state.promoted_at = datetime.now(timezone.utc)
    state.updated_at = datetime.now(timezone.utc)
    session.add(state)
    session.flush()
    return promoted
