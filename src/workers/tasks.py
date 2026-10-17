"""At-least-once Celery delivery with Postgres fencing and bounded recovery."""
from datetime import timedelta
from threading import Event, Thread
from uuid import uuid4

from celery.exceptions import Ignore
from cryptography.fernet import InvalidToken
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlmodel import Session

from src.config import settings
from src.db.database import engine
from src.models.workflow import WorkflowOperation, WorkflowStatus
from src.models.workflow_dispatch import DispatchStatus, WorkflowDispatch
from src.services import monitoring
from src.services import authentication as auth
from src.services.workflow import Operation, _save, now, aware
from src.services.workflow import OperationLeaseExpired
from src.services.workflow_dispatch import fail_background, _record_terminal
from src.services.workflow_failure import failure_from_exception
from src.workers.celery_app import celery_app


def _claim(operation_id, generation, dispatch_id):
    owner = str(uuid4())
    data = caller = None
    try:
        with Session(engine, expire_on_commit=False) as session:
            row = session.get(WorkflowOperation, operation_id, with_for_update=True)
            dispatch = session.get(WorkflowDispatch, dispatch_id, with_for_update=True)
            if (row is None or dispatch is None or row.execution_mode != 'background'
                    or row.generation != generation or dispatch.generation != generation
                    or dispatch.operation_id != operation_id or dispatch.status not in {
                        DispatchStatus.PUBLISHING.value, DispatchStatus.PUBLISHED.value}):
                return None
            if row.status != WorkflowStatus.QUEUED.value:
                # Duplicate delivery, stale generation, or an already claimed job.
                return None
            timestamp = now()
            if row.queue_deadline and aware(row.queue_deadline) <= timestamp:
                _record_terminal(session, row, 'WorkflowQueueTimeout',
                                 'workflow did not start before its queue deadline')
                session.commit()
                return None
            # Decrypt and validate the allowlisted contract, then re-check the
            # initiating identity and permissions before taking worker ownership.
            from src.services.workflow_submission import open_input
            from src.services.workflow_runner import caller_from_input, validate_input
            data = open_input(row.input_envelope)
            validate_input(data, row)
            caller = caller_from_input(data)
            verified = auth.authorize_operation(session, caller, data['kind'], data['scope'], data['payload'])
            timestamp = now()
            if row.queue_deadline and aware(row.queue_deadline) <= timestamp:
                _record_terminal(session, row, 'WorkflowQueueTimeout',
                                 'workflow did not start before its queue deadline')
                session.commit()
                return None
            row.status, row.stage, row.owner = WorkflowStatus.PROCESSING.value, 'claimed', owner
            row.run_deadline = timestamp + timedelta(seconds=settings.WORKFLOW_MAX_RUN_SECONDS)
            row.lease_until = min(timestamp + timedelta(seconds=settings.WORKFLOW_LEASE_SECONDS), row.run_deadline)
            row.queue_deadline, row.next_run_at = None, None
            row.run_count += 1
            row.heartbeat_at = row.updated_at = timestamp
            _save(session, row)
            auth.event(session, verified, 'accepted', 'workflow:worker_claimed')
            session.commit()
            return owner, row.model_copy(deep=True)
    except (HTTPException, ValueError, TypeError, KeyError, InvalidToken, ValidationError) as exc:
        _fail_before_claim(operation_id, generation, dispatch_id, data, caller, exc)
        return None


def _fail_before_claim(operation_id, generation, dispatch_id, data, caller, exc):
    """Persist an auth/contract failure without consuming a worker run."""
    status = exc.status_code if isinstance(exc, HTTPException) else 400
    detail = exc.detail if isinstance(exc, HTTPException) else 'background workflow input is invalid'
    with Session(engine) as session:
        row = session.get(WorkflowOperation, operation_id, with_for_update=True)
        dispatch = session.get(WorkflowDispatch, dispatch_id, with_for_update=True)
        if (row is None or dispatch is None or row.generation != generation
                or dispatch.generation != generation or dispatch.operation_id != operation_id
                or row.status != WorkflowStatus.QUEUED.value):
            return
        failure = failure_from_exception(exc)
        failure.update(http_status=status, detail=detail)
        # This shared projection closes the exact persisted reservation even
        # when the envelope cannot be decrypted; input is never used to guess it.
        fail_background(session, row, failure, retryable=status in {401, 403},
                        reason='worker_authorization_or_input')
        if caller is not None and row.caller_principal_id == caller.principal_id:
            auth.event(session, caller, 'denied', 'workflow:worker_authorization_or_input')
        session.commit()
    attempt = monitoring.Attempt(route=caller.route if caller else 'workflow_worker',
                                 operation_id=operation_id,
                                 correlation_id=caller.correlation_id if caller else None)
    token = monitoring._current.set(attempt)
    try:
        monitoring.record('worker_claim', 'failed', error_type=type(exc).__name__, http_status=status,
                          parser_trace={'operation_id': operation_id, 'generation': generation})
        monitoring.flush_current(engine)
    finally:
        monitoring._current.reset(token)


def _heartbeat(operation_id, generation, owner, stop, lost):
    while not stop.wait(settings.WORKFLOW_HEARTBEAT_SECONDS):
        try:
            with Session(engine) as session:
                row = session.get(WorkflowOperation, operation_id, with_for_update=True)
                timestamp = now()
                if (row is None or row.generation != generation
                        or row.status != WorkflowStatus.PROCESSING.value
                        or row.owner != owner or row.lease_until is None
                        or aware(row.lease_until) <= timestamp
                        or (row.run_deadline and aware(row.run_deadline) <= timestamp)):
                    lost.set()
                    return
                row.heartbeat_at = row.updated_at = timestamp
                row.lease_until = min(timestamp + timedelta(seconds=settings.WORKFLOW_LEASE_SECONDS),
                                      aware(row.run_deadline)) if row.run_deadline else timestamp + timedelta(seconds=settings.WORKFLOW_LEASE_SECONDS)
                _save(session, row)
                session.commit()
        except Exception:
            # A missed heartbeat is recovered by the dispatcher after lease expiry.
            lost.set()
            return


class WorkerHeartbeatLost(RuntimeError):
    """The independent lease keeper lost its PostgreSQL ownership check."""


def _automatic_recovery_eligible(exc):
    """Only transient PostgreSQL failures use whole-workflow retry budget."""
    if isinstance(exc, (WorkerHeartbeatLost, OperationLeaseExpired)):
        return True
    if not isinstance(exc, DBAPIError):
        return False
    original = exc.orig
    sqlstate = getattr(original, 'sqlstate', None) or getattr(original, 'pgcode', None)
    return bool(exc.connection_invalidated or sqlstate in {
        '40001', '40P01', '55P03', '08000', '08003', '08006', '08001', '08004', '08007'
    } or isinstance(exc, OperationalError) and sqlstate is None)


@celery_app.task(name='src.workers.tasks.run_workflow', bind=True, acks_late=True,
                 reject_on_worker_lost=True, ignore_result=True)
def run_workflow(self, operation_id, generation, dispatch_id):
    # Drain already accepted jobs even when new background submissions are disabled.
    claimed = _claim(operation_id, generation, dispatch_id)
    if claimed is None:
        raise Ignore()
    owner, row = claimed
    stop, lost = Event(), Event()
    heartbeat = Thread(target=_heartbeat, args=(operation_id, generation, owner, stop, lost), daemon=True)
    heartbeat.start()
    from src.services import workflow_runner
    attempt = None
    op = Operation(engine, operation_id, owner, generation=generation)
    try:
        result, attempt, op = workflow_runner.run_operation(row, owner, lost)
        stop.set()
        heartbeat.join(timeout=2)
        with Session(engine) as session:
            current = session.get(WorkflowOperation, operation_id)
            if (lost.is_set() and current is not None
                    and current.status != WorkflowStatus.COMPLETED.value
                    and current.owner == owner and current.status == WorkflowStatus.PROCESSING.value):
                raise WorkerHeartbeatLost('worker lease lost while workflow was running')
            still_processing = current is not None and current.owner == owner and current.status == WorkflowStatus.PROCESSING.value
        if still_processing:
            op.transaction('completed', lambda session, checkpoint: result, complete=True)
        if attempt is not None:
            monitoring.persist_incremental(attempt, engine)
    except Exception as exc:
        attempt = getattr(exc, '_monitoring_attempt', attempt)
        token = monitoring._current.set(attempt) if attempt is not None else None
        try:
            try:
                op.fail(exc, automatic_retry=_automatic_recovery_eligible(exc))
            finally:
                if attempt is not None:
                    monitoring.record('workflow', 'failed', error_type=type(exc).__name__,
                                      http_status=getattr(exc, 'status_code', None),
                                      validation_errors=getattr(exc, 'validation_errors', None),
                                      parser_trace={'operation_id': operation_id, 'generation': generation})
                    monitoring.flush_current(engine)
        finally:
            if token is not None:
                monitoring._current.reset(token)
    finally:
        stop.set()
        heartbeat.join(timeout=2)
    return None
