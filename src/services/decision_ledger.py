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
from src.models.decision_contracts import (
    DECISION_WRITE_ADAPTER, DecisionWriteBase, ParserDecisionEvidence,
    ParserDecisionWrite, PolicyDecisionWrite,
)
from src.models.request import ActionRequest, EvaluationResult, NaturalLanguageRequest
from src.services import monitoring


def append_decision(session: Session, payload: DecisionWriteBase) -> DecisionRecord:
    """Validate even internally-created models before a ledger insertion."""
    validated = DECISION_WRITE_ADAPTER.validate_python(payload)
    record = DecisionRecord(**validated.table_values())
    session.add(record)
    session.flush()
    monitoring.link_decision(record.decision_id)
    return record


def validate_policy_record(record: DecisionRecord) -> PolicyDecisionWrite:
    """Reject parser-stage or malformed records at policy-only boundaries."""
    return PolicyDecisionWrite.model_validate(record.model_dump())


def save_policy_record(session: Session, record: DecisionRecord) -> None:
    validate_policy_record(record)
    session.add(record)
    session.flush()


def log_parser_rejection(session: Session, request: NaturalLanguageRequest,
                         reason: str, evidence: ParserDecisionEvidence) -> DecisionRecord:
    source = NaturalLanguageRequest.model_validate(request)
    record = append_decision(session, ParserDecisionWrite(
        request_id=source.request_id, agent_id=source.agent_id, actor_role=source.actor_role,
        request_json=source, reason=reason, evidence_json=evidence,
    ))
    monitoring.record("parser_decision", "denied", decision_id=record.decision_id,
                      validation_errors=evidence.model_dump(mode="json")["validation_errors"])
    return record


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
    # Do not trust model_copy/model_construct or mutable model instances as validation.
    request = ActionRequest.model_validate(request)
    result = EvaluationResult.model_validate(result)
    evidence: Dict[str, Any] = dict(result.evidence)
    if result.blast_radius:
        evidence["blast_radius"] = result.blast_radius

    payload = PolicyDecisionWrite(
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
    return append_decision(session, payload)
