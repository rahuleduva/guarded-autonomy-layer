"""Combined human review and cloud-free authorization finalization."""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4

from sqlmodel import Session, select

from src.models.decision import DecisionRecord
from src.models.enums import DecisionOutcome, EscalationStatus, HumanReviewDecision
from src.models.escalation import EscalationQueueItem
from src.models.policy import Policy
from src.models.human_review import HumanReviewRecord, HumanReviewResponse, HumanReviewWrite
from src.models.request import ActionRequest, ApprovalDecision, EscalationPackage, EvaluationResponse
from src.services.action_binding import fingerprint as action_fingerprint
from src.services.advisory_evidence import autonomy_snapshot
from src.services.decision_ledger import log_decision, save_policy_record, validate_policy_record
from src.services.policy_engine import evaluate
from src.services.policy_store import active, validate_stored
from src.services.review_explanation import explain, package_fingerprint
from src.services import autonomy_service, monitoring, token_issuer


class ReviewConflictError(Exception):
    """A review package is stale, already decided, or mismatched."""


def prepare_package(context: dict) -> EscalationPackage:
    """Pure cloud phase: context contains detached JSON, never ORM objects."""
    from src.db.transaction_guard import assert_no_transaction
    assert_no_transaction()
    package = EscalationPackage.model_validate(context["package"])
    evidence = context["evidence"]
    package = package.model_copy(update={
        "original_policy_hash": context["policy_hash"],
        "original_outcome": DecisionOutcome(context["outcome"]),
        "action_fingerprint": action_fingerprint(package.request),
        "package_version": "human-review-package-v1",
    })
    requirements, explanation = explain(package, evidence, context["rules"])
    package = package.model_copy(update={
        "review_requirements": requirements,
        "explanation": explanation,
        "package_fingerprint": None,
        "status": EscalationStatus.PENDING,
    })
    package = package.model_copy(update={"package_fingerprint": package_fingerprint(package)})
    return EscalationPackage.model_validate(package)


def raise_escalation(session: Session, decision_id: str) -> EscalationQueueItem:
    decision = session.exec(select(DecisionRecord).where(
        DecisionRecord.decision_id == decision_id).with_for_update()).one_or_none()
    if decision is None:
        raise ValueError("decision not found")
    validate_policy_record(decision)
    if decision.outcome not in {DecisionOutcome.ESCALATED.value, DecisionOutcome.PENDING_APPROVAL.value}:
        raise ValueError("only held decisions can enter the review queue")
    existing = session.exec(select(EscalationQueueItem).where(
        EscalationQueueItem.decision_id == decision_id)).one_or_none()
    if existing is not None:
        return existing
    package = EscalationPackage(
        decision_id=decision_id,
        request=ActionRequest.model_validate(decision.request_json),
        risk_level=decision.risk_level,
        blast_radius=decision.evidence_json.get("blast_radius", {}),
        semantic_warning=decision.evidence_json.get("semantic_warning"),
        semantic_findings=decision.evidence_json.get("semantic_pipeline", {}),
        recommended_decision=f"Review before execution: {decision.reason}",
    )
    package = package.model_copy(update={"status": EscalationStatus.PREPARING})
    row = EscalationQueueItem(escalation_id=package.escalation_id, decision_id=decision_id,
                              package_json=package.model_dump(mode="json"),
                              status=EscalationStatus.PREPARING.value)
    session.add(row)
    session.flush()
    return row


def preparation_context(session: Session, decision_id: str) -> dict:
    row = session.exec(select(EscalationQueueItem).where(
        EscalationQueueItem.decision_id == decision_id).with_for_update()).one_or_none()
    resumable_statuses = {
        EscalationStatus.PREPARING.value,
        EscalationStatus.PENDING.value,
        EscalationStatus.APPROVED.value,
        EscalationStatus.REJECTED.value,
    }
    if row is None or row.status not in resumable_statuses:
        raise ReviewConflictError("escalation package is unavailable")
    decision = session.get(DecisionRecord, decision_id)
    if decision is None:
        raise ValueError("decision not found")
    validate_policy_record(decision)
    package = EscalationPackage.model_validate(row.package_json)
    if package.status.value != row.status:
        raise ReviewConflictError("escalation status differs from its stored package")
    if (row.status in {EscalationStatus.APPROVED.value, EscalationStatus.REJECTED.value}
            and not package.package_fingerprint):
        raise ReviewConflictError("a reviewed escalation has no completed review package")
    evidence = decision.evidence_json or {}
    if (package.decision_id != decision.decision_id
            or package.request.model_dump(mode="json") != decision.request_json
            or package.semantic_findings != evidence.get("semantic_pipeline", {})
            or package.semantic_warning != evidence.get("semantic_warning")
            or package.blast_radius != evidence.get("blast_radius", {})):
        raise ReviewConflictError("pending escalation does not match its original decision")
    policy = session.exec(select(Policy).where(Policy.policy_hash == decision.policy_hash)).one_or_none()
    if policy is None:
        raise ValueError("original review policy not found")
    validate_stored(policy)
    # Legacy PENDING packages without explanations become explicitly unavailable
    # to reviewers before leaving this transaction.
    if not package.package_fingerprint:
        row.status = EscalationStatus.PREPARING.value
        package = package.model_copy(update={"status": EscalationStatus.PREPARING})
        row.package_json = package.model_dump(mode="json")
        session.add(row)
    return deepcopy(dict(package=package.model_dump(mode="json"), evidence=evidence,
                         policy_hash=decision.policy_hash, outcome=decision.outcome,
                         rules=policy.rules_json))


def prepare_existing(session: Session, decision_id: str, prepared: EscalationPackage) -> EscalationPackage:
    """Database-only finalization of an explanation generated outside a transaction."""
    context = preparation_context(session, decision_id)
    current = EscalationPackage.model_validate(context['package'])
    prepared = EscalationPackage.model_validate(prepared)
    if current.package_fingerprint:
        # A concurrent preparer may have finalized the package, and a reviewer
        # may already have acted on it while this worker was generating text.
        # Preserve the committed package as the authority and let this workflow
        # finish from that durable state.
        return current
    expected = current.model_copy(update={
        'status': EscalationStatus.PENDING, 'package_version': prepared.package_version,
        'original_outcome': DecisionOutcome(context['outcome']), 'original_policy_hash': context['policy_hash'],
        'action_fingerprint': action_fingerprint(current.request),
        'review_requirements': prepared.review_requirements, 'explanation': prepared.explanation,
        'package_fingerprint': None,
    })
    expected = expected.model_copy(update={'package_fingerprint': package_fingerprint(expected)})
    if expected.model_dump(mode='json') != prepared.model_dump(mode='json'):
        raise ReviewConflictError('prepared explanation no longer matches the stored package')
    row = session.exec(select(EscalationQueueItem).where(
        EscalationQueueItem.decision_id == decision_id)).one()
    row.status, row.package_json = EscalationStatus.PENDING.value, prepared.model_dump(mode='json')
    session.add(row)
    session.flush()
    return prepared


def list_pending(session: Session) -> list[EscalationPackage]:
    return [EscalationPackage.model_validate(row.package_json) for row in session.exec(
        select(EscalationQueueItem).where(EscalationQueueItem.status == EscalationStatus.PENDING.value)
        .order_by(EscalationQueueItem.created_at, EscalationQueueItem.escalation_id)
    ).all()]


def _persist_review(*, row: EscalationQueueItem, original: DecisionRecord, package: EscalationPackage,
                    submission: ApprovalDecision, review_id: str,
                    authorization_decision_id: str | None) -> HumanReviewRecord:
    contract = HumanReviewWrite(
        review_id=review_id,
        escalation_id=row.escalation_id,
        original_decision_id=original.decision_id,
        package_fingerprint=package.package_fingerprint,
        action_fingerprint=package.action_fingerprint,
        reviewer_id=submission.reviewer_id,
        decision=HumanReviewDecision.APPROVE if submission.approve else HumanReviewDecision.REJECT,
        note=submission.note,
        authorization_decision_id=authorization_decision_id,
    )
    review = HumanReviewRecord.from_contract(contract)
    return review


def _finish_approval(session: Session, *, submission: ApprovalDecision,
                     original: DecisionRecord, package: EscalationPackage,
                     review_id: str) -> tuple[EvaluationResponse | None, str, str | None]:
    # Serialize with policy promotion. A changed active policy makes this
    # package stale; no new cloud evaluation is hidden inside the review call.
    policy = active(session)
    validate_stored(policy)
    if policy.policy_hash != original.policy_hash:
        raise ReviewConflictError("active policy changed after this package was created; reevaluate and review a fresh package")

    request = ActionRequest.model_validate(original.request_json).model_copy(update={
        "request_id": str(uuid4()), "timestamp": datetime.now(timezone.utc),
    })
    if action_fingerprint(request) != package.action_fingerprint:
        raise ReviewConflictError("the action no longer matches the reviewed package")

    evidence = deepcopy(original.evidence_json or {})
    original_autonomy = deepcopy(evidence.get("autonomy_snapshot"))
    state = autonomy_service.get(session, request.agent_id, request.action_class)
    evidence["autonomy_snapshot"] = autonomy_snapshot(request, state)
    evidence["human_review_finalization"] = {
        "version": "human_review_finalization_v1",
        "review_id": review_id,
        "escalation_id": package.escalation_id,
        "original_decision_id": original.decision_id,
        "package_fingerprint": package.package_fingerprint,
        "action_fingerprint": package.action_fingerprint,
        "reviewer_id": submission.reviewer_id,
        "review_note": submission.note,
        "original_autonomy_snapshot": original_autonomy,
        "active_policy_hash": policy.policy_hash,
    }
    evidence["review_approval"] = {
        "approved": True,
        "reviewer_id": submission.reviewer_id,
        "original_decision_id": original.decision_id,
        "action_fingerprint": package.action_fingerprint,
        "review_id": review_id,
        "package_fingerprint": package.package_fingerprint,
    }

    # Deterministic policy checks only. The original semantic/blast evidence is
    # deep-copied unchanged; this path does not parse, embed, retrieve, or judge.
    result = evaluate(request, policy, evidence, state)
    decision = log_decision(session, request, result, reviewer_id=submission.reviewer_id)
    token = None
    if result.outcome == DecisionOutcome.ALLOWED:
        token = token_issuer.issue(decision.decision_id, result.policy_hash, request, result.ttl_seconds)
        decision.capability_token_hash = sha256(token.encode()).hexdigest()
        save_policy_record(session, decision)

    evaluation = EvaluationResponse(
        decision_id=decision.decision_id,
        request_id=request.request_id,
        action_request=request,
        decision=result.outcome,
        risk_level=result.risk_level,
        blast_radius=result.blast_radius,
        policy_hash=result.policy_hash,
        reason=result.reason,
        capability_token=token,
        autonomy_level=result.autonomy_level,
    )
    allowed = result.outcome == DecisionOutcome.ALLOWED
    return evaluation, ("ALLOWED" if allowed else "BLOCKED"), decision.decision_id


def decide(session: Session, decision_id: str, submission: ApprovalDecision) -> HumanReviewResponse:
    submission = ApprovalDecision.model_validate(submission)
    if submission.decision_id != decision_id:
        raise ValueError("path and payload decision IDs must match")
    if not submission.reviewer_id.strip():
        raise ValueError("reviewer_id must not be blank")

    row = session.exec(select(EscalationQueueItem).where(
        EscalationQueueItem.decision_id == decision_id).with_for_update()).one_or_none()
    if row is None or row.status != EscalationStatus.PENDING.value:
        raise ReviewConflictError("pending escalation not found or already reviewed")
    original = session.get(DecisionRecord, decision_id)
    if original is None:
        raise ValueError("decision not found")
    validate_policy_record(original)
    if original.outcome not in {DecisionOutcome.ESCALATED.value, DecisionOutcome.PENDING_APPROVAL.value}:
        raise ReviewConflictError("review requires a completed held policy decision")

    package = EscalationPackage.model_validate(row.package_json)
    if (package.status != EscalationStatus.PENDING
            or package.decision_id != original.decision_id
            or package.escalation_id != row.escalation_id
            or package.request.model_dump(mode="json") != original.request_json
            or package.semantic_findings != original.evidence_json.get("semantic_pipeline", {})
            or package.semantic_warning != original.evidence_json.get("semantic_warning")
            or package.blast_radius != original.evidence_json.get("blast_radius", {})
            or row.reviewer_id is not None or row.decided_at is not None):
        raise ReviewConflictError("pending escalation does not match its original decision")
    if not package.package_fingerprint or not package.explanation or not package.review_requirements:
        raise ReviewConflictError("review package is not prepared; fetch a newly prepared package before deciding")
    if submission.package_fingerprint != package.package_fingerprint:
        raise ReviewConflictError("package fingerprint differs from the package shown to the reviewer")
    if package_fingerprint(package) != package.package_fingerprint:
        raise ReviewConflictError("stored review package failed its integrity check")
    if package.action_fingerprint != action_fingerprint(package.request):
        raise ReviewConflictError("stored review package action binding is invalid")

    new_status = EscalationStatus.APPROVED if submission.approve else EscalationStatus.REJECTED
    row.status = new_status.value
    row.reviewer_id = submission.reviewer_id
    row.decided_at = datetime.now(timezone.utc)
    updated = package.model_copy(update={
        "status": new_status,
        "reviewer_id": submission.reviewer_id,
        "review_note": submission.note,
        "decided_at": row.decided_at,
    })
    row.package_json = updated.model_dump(mode="json")
    session.add(row)
    session.flush()

    # Generate the review ID before authorization so both immutable records can
    # refer to one another. Insert the human row only after the linked decision
    # ID is known; the caller-owned transaction commits or rolls back both.
    review_id = str(uuid4())
    evaluation = None
    authorization_status = "NOT_REQUESTED"
    authorization_reason = "The reviewer rejected the package; no authorization was requested."
    authorization_decision_id = None
    if submission.approve:
        try:
            evaluation, authorization_status, authorization_decision_id = _finish_approval(
                session, submission=submission, original=original, package=package,
                review_id=review_id)
        except Exception as exc:
            monitoring.record("human_review", "failed", decision_id=original.decision_id,
                              error_type=type(exc).__name__,
                              parser_trace={"escalation_id": row.escalation_id,
                                            "authorization_finalization": True})
            raise
        authorization_reason = evaluation.reason

    review = _persist_review(row=row, original=original, package=package,
                             submission=submission, review_id=review_id,
                             authorization_decision_id=authorization_decision_id)
    session.add(review)
    session.flush()

    return HumanReviewResponse(
        status=new_status.value,
        review_id=review.review_id,
        original_decision_id=original.decision_id,
        authorization_status=authorization_status,
        authorization_decision_id=authorization_decision_id,
        authorization_reason=authorization_reason,
        evaluation=evaluation.model_dump(mode="json") if evaluation else None,
        capability_token=evaluation.capability_token if evaluation else None,
        new_policy_version=None,
    )
