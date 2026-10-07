"""Fail-closed decision ledger (Phase 1, findings A3 and A7).

The ledger is the audit spine: an append-only record of every decision and
the sole input to replay. A write that fails must abort the request rather
than let an unlogged action proceed, so nothing here catches or swallows
exceptions -- they propagate to the caller by design.

The caller owns the transaction. ``log_decision`` flushes but never commits,
so the ledger write and the action it authorises can share one transaction:
if either side fails, both roll back.
"""

from enum import Enum
from typing import Any, Dict, Optional
from uuid import uuid4

from sqlmodel import Session

from src.models.decision import DecisionRecord
from src.models.request import ActionRequest, EvaluationResult


def _as_str(value: Any) -> str:
    """Persist the plain value of a str-Enum, never its repr.

    ``str()`` on a ``str, Enum`` mixin is version-dependent, so ``.value`` is
    the only stable form to store.
    """
    return value.value if isinstance(value, Enum) else str(value)


def log_decision(
    session: Session,
    request: ActionRequest,
    result: EvaluationResult,
    capability_token_hash: Optional[str] = None,
    reviewer_id: Optional[str] = None,
) -> DecisionRecord:
    """Append one decision to the ledger and return the flushed row.

    Raises whatever the database raises -- notably ``IntegrityError`` on a
    duplicate ``request_id`` -- so that a failed audit write aborts the
    request instead of silently leaving it unlogged (A3).
    """
    evidence: Dict[str, Any] = dict(result.evidence)
    if result.blast_radius:
        evidence["blast_radius"] = result.blast_radius

    record = DecisionRecord(
        decision_id=str(uuid4()),
        request_id=request.request_id,
        agent_id=request.agent_id,
        actor_role=request.actor_role,
        action_class=request.action_class,
        target_resource=request.target_resource,
        request_json=request.model_dump(mode="json"),
        risk_level=_as_str(result.risk_level),
        autonomy_level=_as_str(result.autonomy_level),
        outcome=_as_str(result.outcome),
        policy_hash=result.policy_hash,
        reason=result.reason,
        evidence_json=evidence,
        capability_token_hash=capability_token_hash,
        reviewer_id=reviewer_id,
    )
    session.add(record)
    session.flush()
    return record
