"""Ownership-checked human review progress and original capability retrieval."""
import json

from fastapi import HTTPException
from sqlmodel import select

from src.models.enums import EscalationStatus
from src.models.escalation import EscalationQueueItem
from src.models.human_review import HumanReviewRecord
from src.models.request import AgentReviewStatus, EvaluationResponse
from src.models.workflow import WorkflowOperation, WorkflowStatus
from src.services import authentication as auth


def evaluation_for_review(session, caller, review):
    from src.services.workflow import _unpack
    auth.own_decision(session, caller, review.authorization_decision_id)
    operation = session.exec(select(WorkflowOperation).where(
        WorkflowOperation.kind == 'review', WorkflowOperation.status == WorkflowStatus.COMPLETED.value,
        WorkflowOperation.checkpoint['review_id'].as_string() == review.review_id)).one_or_none()
    if operation is None:
        # Older reviews may predate encrypted capability storage. Never mint a
        # replacement or report a completed review as still pending.
        raise HTTPException(503, 'recorded authorization response is unavailable')
    value = json.loads(_unpack(operation.response).body).get('evaluation')
    if not value or value.get('decision_id') != review.authorization_decision_id:
        raise HTTPException(503, 'recorded authorization response is unavailable')
    evaluation = EvaluationResponse.model_validate(value)
    if (evaluation.action_request.agent_id, evaluation.action_request.actor_role) != (caller.agent_id, caller.actor_role):
        raise HTTPException(503, 'recorded authorization response is inconsistent')
    return evaluation


def status(session, caller, original_decision_id):
    caller = auth.refresh(session, caller)
    auth.require(caller, 'read_own')
    auth.own_decision(session, caller, original_decision_id)
    # Serialize the short read with review finalization so the package status,
    # review audit, and authorization response form one consistent view.
    escalation = session.exec(select(EscalationQueueItem).where(
        EscalationQueueItem.decision_id == original_decision_id).with_for_update()).one_or_none()
    if escalation is None:
        raise HTTPException(404, 'review request not found')
    state = EscalationStatus(escalation.status)
    if state in {EscalationStatus.PREPARING, EscalationStatus.PENDING}:
        return AgentReviewStatus(original_decision_id=original_decision_id,
            status='PREPARING' if state == EscalationStatus.PREPARING else 'AWAITING_REVIEW',
            reason='Review package is being prepared.' if state == EscalationStatus.PREPARING
                   else 'Waiting for human review.')
    review = session.exec(select(HumanReviewRecord).where(
        HumanReviewRecord.escalation_id == escalation.escalation_id)).one_or_none()
    if review is None or review.original_decision_id != original_decision_id:
        raise HTTPException(503, 'recorded human review is unavailable')
    if state == EscalationStatus.REJECTED:
        if review.decision != 'REJECT' or review.authorization_decision_id is not None:
            raise HTTPException(503, 'recorded human review is inconsistent')
        return AgentReviewStatus(original_decision_id=original_decision_id, status='REJECTED',
                                 reason='Human reviewer rejected the request.')
    if review.decision != 'APPROVE' or not review.authorization_decision_id:
        raise HTTPException(503, 'recorded human review is inconsistent')
    evaluation = evaluation_for_review(session, caller, review)
    return AgentReviewStatus(original_decision_id=original_decision_id,
        status='APPROVED_ALLOWED' if evaluation.decision == 'ALLOWED' else 'APPROVED_BLOCKED',
        reason=evaluation.reason, authorization_decision_id=evaluation.decision_id, evaluation=evaluation)
