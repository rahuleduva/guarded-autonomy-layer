"""Dual evaluation for policy shadow mode (Phase 3).

The active policy decides the real outcome; the candidate policy is evaluated
through the **same full pipeline** (including the autonomy gate) so its verdict
is directly comparable. The candidate's outcome is written to
``shadow_records.would_have_decided``, linked to the real decision by
``decision_id``.
"""

import copy
from typing import Any, Dict, Optional

from sqlmodel import Session, select

from src.models.decision import DecisionRecord
from src.models.policy import Policy
from src.models.policy_shadow import ShadowRecord
from src.models.request import ActionRequest, EvaluationResult
from src.models.autonomy import AutonomyState
from src.services.policy_engine import evaluate
from src.services.decision_ledger import log_decision
from src.services.advisory_evidence import autonomy_snapshot, build_evidence
from src.services import autonomy_service
from src.services.policy_store import validate_stored


def evaluate_with_active_and_shadow_policy(
    request: ActionRequest,
    active_policy: Policy,
    candidate_policy: Policy,
    evidence: Optional[Dict[str, Any]] = None,
    autonomy_state: Optional[AutonomyState] = None,
) -> tuple[EvaluationResult, str]:
    """
    Evaluate ``request`` against both policies.

    Returns
    -------
    active_result : EvaluationResult
        The real decision from the active policy (decides execution).
    would_have_decided : str
        The candidate policy's outcome after the full pipeline (autonomy gate
        included). One of ALLOWED, DENIED, ESCALATED, PENDING_APPROVAL,
        SHADOW_LOGGED — mirrors ``decision_records.outcome``.
    """
    # Evaluate active policy (the real decision)
    active_result = evaluate(request, active_policy, evidence, autonomy_state)

    # Evaluate candidate policy through the SAME pipeline (including autonomy gate)
    candidate_result = evaluate(request, candidate_policy, evidence, autonomy_state)

    # Return active decision + candidate's full outcome
    return active_result, candidate_result.outcome.value


def evaluate_and_record(
    session: Session,
    request: ActionRequest,
    evidence: Optional[Dict[str, Any]] = None,
    autonomy_state: Optional[AutonomyState] = None,
) -> tuple[EvaluationResult, DecisionRecord, Optional[ShadowRecord]]:
    """Evaluate stored active/candidate policies and stage their audit records.

    An active policy is mandatory; a shadow candidate is optional. Only the
    active result is returned as the real decision. Both evaluations receive
    the same request, evidence, and autonomy snapshot. This service issues no
    token and executes no action. Without an explicitly supplied trusted
    autonomy snapshot, it loads persisted state and updates its ledger-backed
    streak after both audits succeed. Explicit snapshots are a service/testing
    seam; callers must never populate them from client control flags.

    Writes flush without committing, matching the decision ledger convention.
    The caller must commit both records together, or roll back on any error.
    Database failures propagate so a failed candidate audit cannot be ignored.
    """
    active_policy = session.exec(
        select(Policy).where(Policy.is_active == True)
    ).one_or_none()
    if active_policy is None:
        raise ValueError("cannot evaluate an action without an active policy")
    candidate_policy = session.exec(
        select(Policy).where(Policy.is_shadow == True)
    ).one_or_none()
    if candidate_policy is not None and candidate_policy.policy_hash == active_policy.policy_hash:
        raise ValueError("active policy cannot also be the shadow candidate")
    validate_stored(active_policy)
    if candidate_policy is not None:
        validate_stored(candidate_policy)

    track_autonomy = autonomy_state is None
    if track_autonomy:
        autonomy_state = autonomy_service.get(session, request.agent_id, request.action_class)

    # Collect once for active/candidate parity. Supplied advisory values are
    # preserved; the actual environment and autonomy gate inputs are frozen
    # here so the audit never contains a stale or client-asserted snapshot.
    if evidence is None:
        evidence = build_evidence(request, autonomy_state)
    else:
        evidence = copy.deepcopy(evidence)
        evidence["environment"] = request.environment
        evidence["autonomy_snapshot"] = autonomy_snapshot(request, autonomy_state)

    if candidate_policy is None:
        active_result = evaluate(request, active_policy, evidence, autonomy_state)
        candidate_outcome = None
    else:
        active_result, candidate_outcome = evaluate_with_active_and_shadow_policy(
            request, active_policy, candidate_policy, evidence, autonomy_state
        )

    decision = log_decision(session, request, active_result)
    shadow_record = None
    if candidate_policy is not None:
        shadow_record = ShadowRecord(
            decision_id=decision.decision_id,
            candidate_policy_hash=candidate_policy.policy_hash,
            would_have_decided=candidate_outcome,
        )
        session.add(shadow_record)
        session.flush()
    if track_autonomy:
        autonomy_service.record_outcome(session, request.agent_id, request.action_class)
    return active_result, decision, shadow_record
