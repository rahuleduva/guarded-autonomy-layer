"""Database-only workflow failure audit and interpretation transitions."""
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from src.models.workflow import WorkflowFailure


def failure_from_exception(exc):
    from src.services.escalation_service import ReviewConflictError
    conflict = isinstance(exc, ReviewConflictError)
    status = (exc.status_code if isinstance(exc, HTTPException) else
              409 if isinstance(exc, IntegrityError) or conflict else
              400 if isinstance(exc, ValueError) else 503)
    chain, seen, current = [], set(), exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append({'error_type': type(current).__name__, 'message': str(current)})
        current = current.__cause__ or current.__context__
    return WorkflowFailure(error_type=type(exc).__name__, http_status=status,
        detail=exc.detail if isinstance(exc, HTTPException) else
               str(exc) if status == 400 or conflict else 'operation failed; retry with the same key',
        error_message=str(exc), exception_chain=chain).model_dump(mode='json')


def audit_failure(session, row, failure, *, outcome, reason):
    """Commit complete failure evidence with the state change, including retries."""
    from src.models.monitoring import RequestAttemptEvent
    from src.services import monitoring
    failure = WorkflowFailure.model_validate(failure).model_dump(mode='json')
    attempt = monitoring._current.get()
    trace = {'operation_id': row.operation_id, 'generation': row.generation,
             'run_count': row.run_count, 'automatic_retry_count': row.automatic_retry_count,
             'previous_stage': row.stage, 'failure': failure, 'reason': reason}
    if attempt and attempt.correlation_id:
        trace['correlation_id'] = attempt.correlation_id
    if attempt:
        trace['source_attempt_id'] = attempt.attempt_id
    session.add(RequestAttemptEvent(operation_id=row.operation_id,
        attempt_id=str(uuid4()), sequence=1,
        route=attempt.route if attempt else 'workflow_scheduler',
        stage='workflow_failure', status=outcome,
        error_type=failure['error_type'], http_status=failure['http_status'],
        parser_trace=trace))


def close_interpretation(session, row, failure):
    """Close only the exact turn reserved by this workflow's checkpoint."""
    from src.models.clarification import RequestInterpretation, InterpretationStatus, InterpretationWrite
    checkpoint = row.checkpoint or {}
    if not checkpoint.get('interpretation_id'):
        return
    interpretation = session.get(RequestInterpretation, checkpoint['interpretation_id'],
                                 with_for_update=True, populate_existing=True)
    source = checkpoint.get('source') or {}
    turns = deepcopy(interpretation.turns) if interpretation else []
    current = turns[-1] if turns else {}
    if (interpretation is None or interpretation.status != InterpretationStatus.PROCESSING.value
            or interpretation.revision != checkpoint.get('revision')
            or current.get('request_id') != source.get('request_id')):
        return
    interpretation.status = InterpretationStatus.PROCESSING_FAILED.value
    current.update(status=interpretation.status, error_type=failure['error_type'],
                   error_message=failure.get('error_message') or str(failure['detail']))
    interpretation.turns = turns
    interpretation.updated_at = datetime.now(timezone.utc)
    InterpretationWrite.model_validate(interpretation.model_dump())
    session.add(interpretation)
