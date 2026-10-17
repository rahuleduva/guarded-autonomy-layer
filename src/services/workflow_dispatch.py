"""PostgreSQL outbox publication and atomic, bounded workflow recovery."""
from datetime import timedelta
import json
import logging
import re
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from sqlalchemy import and_, or_, func
from sqlmodel import Session, select

from src.config import settings
from src.db.database import engine
from src.db.transaction_guard import assert_no_transaction
from src.models.workflow import WorkflowOperation, WorkflowStatus, WorkflowFailure
from src.models.workflow_dispatch import DispatchStatus, WorkflowDispatch, save_dispatch
from src.services.workflow import aware, digest, now, _save
from src.services.workflow_failure import audit_failure, close_interpretation

logger = logging.getLogger(__name__)


def _backoff(attempt):
    return min(settings.WORKFLOW_RETRY_BASE_SECONDS * (2 ** max(0, attempt - 1)),
               settings.WORKFLOW_RETRY_MAX_SECONDS)


def _cancel_current_dispatch(session, row):
    dispatch = session.exec(select(WorkflowDispatch).where(
        WorkflowDispatch.operation_id == row.operation_id,
        WorkflowDispatch.generation == row.generation).with_for_update()).one_or_none()
    if dispatch is not None:
        dispatch.status, dispatch.publisher_owner = DispatchStatus.CANCELLED.value, None
        dispatch.publisher_lease_until = None
        dispatch.updated_at = now()
        save_dispatch(session, dispatch)


def fail_background(session, row, failure, *, retryable, automatic_retry=False,
                    reason='worker_failure'):
    """Caller locks the operation. Audit, fencing, and new intent commit together."""
    failure = WorkflowFailure.model_validate(failure).model_dump(mode='json')
    recovering = automatic_retry and row.automatic_retry_count < settings.WORKFLOW_MAX_AUTOMATIC_RETRIES
    if automatic_retry and not recovering:
        failure['detail'] = 'automatic recovery budget exhausted; inspect the recorded failure history'
        retryable = False
    audit_failure(session, row, failure, outcome='retry_wait' if recovering else 'failed', reason=reason)
    _cancel_current_dispatch(session, row)
    timestamp = now()
    row.owner = row.lease_until = row.run_deadline = row.queue_deadline = row.heartbeat_at = None
    row.next_run_at = None
    row.response, row.error, row.updated_at = None, failure, timestamp
    if recovering:
        row.automatic_retry_count += 1
        row.generation += 1
        row.status, row.stage, row.retryable = WorkflowStatus.RETRY_WAIT.value, 'retry_wait', True
        row.next_run_at = timestamp + timedelta(seconds=_backoff(row.automatic_retry_count))
        _save(session, row)
        save_dispatch(session, WorkflowDispatch(
            dispatch_id=digest([row.operation_id, row.generation]),
            operation_id=row.operation_id, generation=row.generation,
            status=DispatchStatus.PENDING.value, available_at=row.next_run_at))
    else:
        row.status, row.stage, row.retryable = WorkflowStatus.FAILED.value, 'failed', retryable
        close_interpretation(session, row, failure)
        _save(session, row)


def _record_terminal(session, row, kind, detail):
    # Queue exhaustion is eligible for an explicit authenticated resubmission.
    fail_background(session, row, dict(error_type=kind, http_status=503, detail=detail,
        error_message=detail, exception_chain=[]), retryable=True, reason='queue_timeout')


def claim_dispatch(session, timestamp):
    # Candidate reads do not lock dispatch rows. All mutation paths lock the
    # operation first, then its dispatch, including multiple dispatchers.
    candidates = session.exec(select(WorkflowDispatch.operation_id, WorkflowDispatch.dispatch_id).where(
        WorkflowDispatch.status == DispatchStatus.PENDING.value,
        WorkflowDispatch.available_at <= timestamp).order_by(
            WorkflowDispatch.available_at, WorkflowDispatch.dispatch_id)
        .limit(settings.WORKFLOW_DISPATCH_BATCH_SIZE)).all()
    for operation_id, dispatch_id in candidates:
        operation = session.exec(select(WorkflowOperation).where(
            WorkflowOperation.operation_id == operation_id).with_for_update(skip_locked=True)).one_or_none()
        if operation is None:
            continue
        row = session.exec(select(WorkflowDispatch).where(
            WorkflowDispatch.dispatch_id == dispatch_id).with_for_update(skip_locked=True)).one_or_none()
        if row is None or row.status != DispatchStatus.PENDING.value or aware(row.available_at) > timestamp:
            continue
        if (operation.execution_mode != 'background' or operation.generation != row.generation
                or operation.status not in {WorkflowStatus.QUEUED.value, WorkflowStatus.RETRY_WAIT.value}):
            row.status, row.publisher_owner, row.publisher_lease_until = DispatchStatus.CANCELLED.value, None, None
            row.updated_at = timestamp
            save_dispatch(session, row)
            session.commit()
            continue
        if operation.status == WorkflowStatus.RETRY_WAIT.value:
            if aware(operation.next_run_at) > timestamp:
                continue
            operation.status, operation.stage = WorkflowStatus.QUEUED.value, 'queued'
            operation.error, operation.retryable, operation.next_run_at = None, False, None
            operation.queue_deadline = timestamp + timedelta(seconds=settings.WORKFLOW_QUEUE_TIMEOUT_SECONDS)
            operation.updated_at = timestamp
            _save(session, operation)
        elif aware(operation.queue_deadline) <= timestamp:
            _record_terminal(session, operation, 'WorkflowQueueTimeout',
                             'workflow did not start before its queue deadline')
            session.commit()
            continue
        row.status, row.publisher_owner = DispatchStatus.PUBLISHING.value, str(uuid4())
        row.publisher_lease_until = timestamp + timedelta(seconds=max(
            settings.WORKFLOW_LEASE_SECONDS,
            2 * (settings.CLOUDAMQP_CONNECTION_TIMEOUT_SECONDS + settings.CLOUDAMQP_PUBLISH_TIMEOUT_SECONDS)))
        row.publish_attempts += 1
        row.updated_at = timestamp
        save_dispatch(session, row)
        # Snapshot while we still own the locked row. An expiring ORM session
        # could otherwise reload a successor's publisher token after commit.
        claim = (row.dispatch_id, row.operation_id, row.generation, row.publisher_owner)
        session.commit()
        return claim
    return None


def _publish_failure(exc):
    """Keep the diagnostic cause chain without storing broker credentials."""
    from src.services.workflow_failure import failure_from_exception
    broker_url = settings.CLOUDAMQP_URL.get_secret_value().strip()
    parsed = urlsplit(broker_url)
    secrets = {broker_url, parsed.username, parsed.password}
    secrets.update(unquote(value) for value in tuple(secrets) if value)
    secret_pattern = '|'.join(re.escape(value) for value in
                             sorted((value for value in secrets if value), key=len, reverse=True))

    def redact(value):
        if isinstance(value, dict):
            return {key: redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, str):
            patterns = [r'(?i:amqps?://)[^\s/@]+@']
            if secret_pattern:
                patterns.insert(0, secret_pattern)
            return re.sub('|'.join(patterns), '[redacted]', value)
        return value

    return redact(failure_from_exception(exc))


def _publish_one(app, claim):
    dispatch_id, operation_id, generation, owner = claim
    try:
        assert_no_transaction()
        # Broker I/O is bounded and receives opaque database identifiers only.
        app.send_task('src.workers.tasks.run_workflow', args=[operation_id, generation, dispatch_id],
            task_id=dispatch_id, queue=settings.WORKFLOW_QUEUE_NAME,
            argsrepr='(<operation-id>, <generation>, <dispatch-id>)', retry=False,
            timeout=settings.CLOUDAMQP_PUBLISH_TIMEOUT_SECONDS,
            confirm_timeout=settings.CLOUDAMQP_PUBLISH_TIMEOUT_SECONDS)
    except Exception as exc:
        from src.models.monitoring import RequestAttemptEvent
        failure = _publish_failure(exc)
        trace = {'dispatch_id': dispatch_id, 'generation': generation, 'failure': failure}
        # Keep a sanitized fallback even if PostgreSQL is unavailable while
        # recording the broker failure. Every failed physical publish is audited.
        logger.warning('workflow_dispatch_publish_failed %s', json.dumps(
            {'operation_id': operation_id, **trace}, separators=(',', ':')))
        with Session(engine) as session:
            row = session.get(WorkflowDispatch, dispatch_id, with_for_update=True)
            if row and row.operation_id == operation_id and row.generation == generation:
                trace.update(publish_attempts=row.publish_attempts,
                             claim_current=row.status == DispatchStatus.PUBLISHING.value
                             and row.publisher_owner == owner)
                session.add(RequestAttemptEvent(operation_id=operation_id,
                    attempt_id=str(uuid4()), sequence=1, route='workflow_dispatcher',
                    stage='workflow_publish', status='failed',
                    error_type=failure['error_type'], parser_trace=trace))
                if trace['claim_current']:
                    row.status, row.publisher_owner, row.publisher_lease_until = DispatchStatus.PENDING.value, None, None
                    row.last_error = 'broker_unavailable'
                    row.available_at = now() + timedelta(seconds=_backoff(row.publish_attempts))
                    row.updated_at = now()
                    save_dispatch(session, row)
                session.commit()
        return False
    with Session(engine) as session:
        row = session.get(WorkflowDispatch, dispatch_id, with_for_update=True)
        if row and row.status == DispatchStatus.PUBLISHING.value and row.publisher_owner == owner:
            row.status, row.publisher_owner, row.publisher_lease_until = DispatchStatus.PUBLISHED.value, None, None
            row.published_at = row.updated_at = now()
            row.last_error = None
            save_dispatch(session, row)
            session.commit()
    return True


def publish_due(app, limit=None):
    published = 0
    for _ in range(limit if limit is not None else settings.WORKFLOW_DISPATCH_BATCH_SIZE):
        with Session(engine) as session:
            claim = claim_dispatch(session, now())
        if claim is None:
            break
        published += int(_publish_one(app, claim))
    return published


def recover_stale():
    """Scan expired operations before applying the batch limit; never renew them."""
    recovered = 0
    timestamp = now()
    with Session(engine) as session:
        rows = session.exec(select(WorkflowOperation).where(
            WorkflowOperation.execution_mode == 'background',
            or_(and_(WorkflowOperation.status == WorkflowStatus.QUEUED.value,
                     WorkflowOperation.queue_deadline <= timestamp),
                and_(WorkflowOperation.status == WorkflowStatus.PROCESSING.value,
                     or_(WorkflowOperation.lease_until <= timestamp,
                         WorkflowOperation.run_deadline <= timestamp))))
            .order_by(func.coalesce(WorkflowOperation.queue_deadline,
                                    WorkflowOperation.lease_until), WorkflowOperation.operation_id)
            .with_for_update(skip_locked=True).limit(settings.WORKFLOW_DISPATCH_BATCH_SIZE)).all()
        for row in rows:
            if row.status == WorkflowStatus.QUEUED.value:
                _record_terminal(session, row, 'WorkflowQueueTimeout',
                                 'workflow did not start before its queue deadline')
            else:
                expired_run = row.run_deadline and aware(row.run_deadline) <= timestamp
                kind = 'WorkflowRunDeadlineExceeded' if expired_run else 'WorkflowLeaseExpired'
                detail = 'worker run deadline expired' if expired_run else 'worker lease expired'
                fail_background(session, row, dict(error_type=kind, http_status=503,
                    detail=detail, error_message=detail, exception_chain=[]),
                    retryable=True, automatic_retry=True, reason='abandoned_worker')
            recovered += 1
        session.commit()

    # Lock no dispatch while waiting for an operation lock. Recheck candidates
    # after operation-first acquisition so racing claims/publications are safe.
    with Session(engine) as session:
        candidates = session.exec(select(WorkflowDispatch.operation_id, WorkflowDispatch.dispatch_id).where(or_(
            and_(WorkflowDispatch.status == DispatchStatus.PUBLISHING.value,
                 WorkflowDispatch.publisher_lease_until <= timestamp),
            and_(WorkflowDispatch.status == DispatchStatus.PUBLISHED.value,
                 WorkflowDispatch.published_at <= timestamp - timedelta(seconds=settings.WORKFLOW_REDELIVERY_CHECK_SECONDS))))
            .order_by(WorkflowDispatch.updated_at, WorkflowDispatch.dispatch_id)
            .limit(settings.WORKFLOW_DISPATCH_BATCH_SIZE)).all()
    for operation_id, dispatch_id in candidates:
        with Session(engine) as session:
            operation = session.exec(select(WorkflowOperation).where(
                WorkflowOperation.operation_id == operation_id).with_for_update(skip_locked=True)).one_or_none()
            if operation is None:
                continue
            dispatch = session.exec(select(WorkflowDispatch).where(
                WorkflowDispatch.dispatch_id == dispatch_id).with_for_update(skip_locked=True)).one_or_none()
            if dispatch is None:
                continue
            stale = (dispatch.status == DispatchStatus.PUBLISHING.value
                     and aware(dispatch.publisher_lease_until) <= timestamp or
                     dispatch.status == DispatchStatus.PUBLISHED.value
                     and aware(dispatch.published_at) <= timestamp - timedelta(seconds=settings.WORKFLOW_REDELIVERY_CHECK_SECONDS))
            if not stale:
                continue
            if (operation.execution_mode != 'background' or operation.generation != dispatch.generation
                    or operation.status not in {WorkflowStatus.QUEUED.value, WorkflowStatus.RETRY_WAIT.value}):
                dispatch.status = DispatchStatus.CANCELLED.value
            else:
                dispatch.status, dispatch.available_at = DispatchStatus.PENDING.value, timestamp
            dispatch.publisher_owner = dispatch.publisher_lease_until = None
            dispatch.updated_at = timestamp
            save_dispatch(session, dispatch)
            session.commit()
    return recovered
