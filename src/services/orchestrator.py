"""Transactional request orchestration; callers commit before releasing tokens."""
import hashlib
from sqlmodel import Session
from src.models.enums import DecisionOutcome
from src.models.request import ActionRequest, EvaluationResponse
from src.services import autonomy_service, monitoring, token_issuer
from src.services.advisory_evidence import build_evidence
from src.services.escalation_service import raise_escalation
from src.services.shadow_engine import evaluate_and_record
from src.services.decision_ledger import save_policy_record


def evaluate_action(session: Session, request: ActionRequest, *,
                    review_approval: dict | None = None, compensation: dict | None = None) -> EvaluationResponse:
    analysis = request._semantic_analysis
    request = ActionRequest.model_validate(request)
    request._semantic_analysis = analysis
    with monitoring.span("autonomy_lookup"):
        state = autonomy_service.get(session, request.agent_id, request.action_class)
    evidence = build_evidence(request, state)
    if review_approval is not None:
        evidence["review_approval"] = review_approval
    if compensation is not None:
        evidence["compensation"] = compensation
    with monitoring.span("decision") as event:
        result, decision, _ = evaluate_and_record(session, request, evidence, state)
        event["decision_id"] = decision.decision_id
        if result.reason.startswith("evaluation failed closed: "):
            event.update(status="degraded",
                         error_type=result.reason.removeprefix("evaluation failed closed: "))
    if result.outcome in {DecisionOutcome.ESCALATED, DecisionOutcome.PENDING_APPROVAL}:
        with monitoring.span("escalation"):
            raise_escalation(session, decision.decision_id)
    with monitoring.span("autonomy_record"):
        autonomy_service.record_outcome(session, request.agent_id, request.action_class)
    token = None
    if result.outcome == DecisionOutcome.ALLOWED:
        with monitoring.span("token_issue"):
            token = token_issuer.issue(decision.decision_id, result.policy_hash, request, result.ttl_seconds)
            decision.capability_token_hash = hashlib.sha256(token.encode()).hexdigest()
            save_policy_record(session, decision)
    return EvaluationResponse(
        status="ok", decision_id=decision.decision_id, request_id=request.request_id,
        action_request=request, decision=result.outcome, risk_level=result.risk_level,
        blast_radius=result.blast_radius, policy_hash=result.policy_hash, reason=result.reason,
        capability_token=token, autonomy_level=result.autonomy_level,
        semantic_summary=evidence.get('semantic_pipeline', {}).get('summary'),
        semantic_mode=(evidence['semantic_pipeline']['mode']
                       if evidence.get('semantic_pipeline', {}).get('summary') is not None else None),
    )
