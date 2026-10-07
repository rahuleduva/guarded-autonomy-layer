"""Held-action packages, recorded human review, and policy version bumps."""
from datetime import datetime, timezone
from uuid import uuid4
from sqlmodel import Session, select
from src.models.decision import DecisionRecord
from src.models.enums import DecisionOutcome, EscalationStatus
from src.models.escalation import EscalationQueueItem
from src.models.request import ActionRequest, EscalationPackage
from src.services.action_binding import fingerprint
from src.services.policy_store import reviewer_bump


def raise_escalation(session: Session, decision_id: str) -> EscalationQueueItem:
    decision = session.exec(select(DecisionRecord).where(
        DecisionRecord.decision_id == decision_id).with_for_update()).one_or_none()
    if decision is None:
        raise ValueError("decision not found")
    if decision.outcome not in {DecisionOutcome.ESCALATED.value, DecisionOutcome.PENDING_APPROVAL.value}:
        raise ValueError("only held decisions can enter the review queue")
    existing = session.exec(select(EscalationQueueItem).where(
        EscalationQueueItem.decision_id == decision_id)).one_or_none()
    if existing is not None:
        return existing
    package = EscalationPackage(
        decision_id=decision_id, request=ActionRequest.model_validate(decision.request_json),
        risk_level=decision.risk_level, blast_radius=decision.evidence_json.get("blast_radius", {}),
        semantic_warning=decision.evidence_json.get("semantic_warning"),
        recommended_decision=f"Review before execution: {decision.reason}",
    )
    row = EscalationQueueItem(escalation_id=package.escalation_id, decision_id=decision_id,
                             package_json=package.model_dump(mode="json"))
    session.add(row)
    session.flush()
    return row


def list_pending(session: Session) -> list[EscalationPackage]:
    return [EscalationPackage.model_validate(row.package_json) for row in session.exec(
        select(EscalationQueueItem).where(EscalationQueueItem.status == EscalationStatus.PENDING.value)
        .order_by(EscalationQueueItem.created_at, EscalationQueueItem.escalation_id)
    ).all()]


def decide(session: Session, decision_id: str, reviewer_id: str, approve: bool,
           note: str | None = None) -> dict:
    if not reviewer_id.strip() or not isinstance(approve, bool):
        raise ValueError("reviewer ID and boolean approve are required")
    row = session.exec(select(EscalationQueueItem).where(
        EscalationQueueItem.decision_id == decision_id).with_for_update()).one_or_none()
    if row is None or row.status != EscalationStatus.PENDING.value:
        raise ValueError("pending escalation not found or already reviewed")
    original = session.get(DecisionRecord, decision_id)
    row.status = EscalationStatus.APPROVED.value if approve else EscalationStatus.REJECTED.value
    row.reviewer_id, row.decided_at = reviewer_id, datetime.now(timezone.utc)
    package = dict(row.package_json)
    package.update(status=row.status, reviewer_id=reviewer_id, review_note=note,
                   decided_at=row.decided_at.isoformat())
    row.package_json = package
    original.reviewer_id = reviewer_id
    session.add(row)
    session.add(original)
    response = {"status": row.status, "new_policy_version": None, "evaluation": None}
    if approve:
        bumped = reviewer_bump(session, reviewer_id)
        request = ActionRequest.model_validate(original.request_json)
        request = request.model_copy(update={"request_id": str(uuid4()), "timestamp": datetime.now(timezone.utc)})
        approval = {"approved": True, "reviewer_id": reviewer_id,
                    "original_decision_id": decision_id, "action_fingerprint": fingerprint(request)}
        from src.services.orchestrator import evaluate_action
        response["new_policy_version"] = bumped.version
        response["evaluation"] = evaluate_action(
            session, request, review_approval=approval,
            compensation=original.evidence_json.get("compensation"),
        ).model_dump(mode="json")
    session.flush()
    return response
