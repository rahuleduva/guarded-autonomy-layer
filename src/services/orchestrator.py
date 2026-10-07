"""Transactional request orchestration; callers commit before releasing tokens."""
import hashlib
from sqlmodel import Session
from src.models.enums import DecisionOutcome
from src.models.request import ActionRequest, EvaluationResponse
from src.services import autonomy_service, token_issuer
from src.services.advisory_evidence import build_evidence
from src.services.escalation_service import raise_escalation
from src.services.shadow_engine import evaluate_and_record


def evaluate_action(session: Session, request: ActionRequest, *,
                    review_approval: dict | None = None, compensation: dict | None = None) -> EvaluationResponse:
    state = autonomy_service.get(session, request.agent_id, request.action_class)
    evidence = build_evidence(request, state)
    if review_approval is not None:
        evidence["review_approval"] = review_approval
    if compensation is not None:
        evidence["compensation"] = compensation
    result, decision, _ = evaluate_and_record(session, request, evidence, state)
    if result.outcome in {DecisionOutcome.ESCALATED, DecisionOutcome.PENDING_APPROVAL}:
        raise_escalation(session, decision.decision_id)
    autonomy_service.record_outcome(session, request.agent_id, request.action_class)
    token = None
    if result.outcome == DecisionOutcome.ALLOWED:
        token = token_issuer.issue(decision.decision_id, result.policy_hash, request, result.ttl_seconds)
        decision.capability_token_hash = hashlib.sha256(token.encode()).hexdigest()
        session.add(decision)
        session.flush()
    return EvaluationResponse(
        status="ok", decision_id=decision.decision_id, request_id=request.request_id,
        action_request=request, decision=result.outcome, risk_level=result.risk_level,
        blast_radius=result.blast_radius, policy_hash=result.policy_hash, reason=result.reason,
        capability_token=token, autonomy_level=result.autonomy_level,
    )
