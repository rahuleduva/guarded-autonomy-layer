"""Own short transactions; never lend a database session to slow work."""
import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlmodel import Session, select

from src.config import settings
from src.db.transaction_guard import assert_no_transaction
from src.models.workflow import WorkflowOperation, WorkflowStatus, WorkflowWrite, WorkflowProgress


class OperationLeaseExpired(HTTPException):
    """A worker lost its lease and may safely retry under a new owner."""

    def __init__(self):
        super().__init__(status_code=409, detail='operation ownership expired; retry with the same idempotency key')


class RetryableWorkflowConflict(HTTPException):
    """A temporary workflow state may change before the same request is retried."""

    def __init__(self, detail):
        super().__init__(status_code=409, detail=detail)


def _is_retryable(exc, status):
    if isinstance(exc, HTTPException) and status in {401, 403}:
        # Resume only after the caller authenticates again and current access
        # checks pass. Expiry/revocation must not corrupt an interpretation turn.
        return True
    if isinstance(exc, (OperationLeaseExpired, RetryableWorkflowConflict)):
        return True
    # A conflict is usually a completed business/state decision. Do not keep
    # rerunning it as though another attempt could fix a stale review or input.
    if isinstance(exc, (HTTPException, IntegrityError)):
        return isinstance(exc, HTTPException) and status in {408, 425, 429} or status >= 500
    if isinstance(exc, DBAPIError):
        original = exc.orig
        sqlstate = getattr(original, 'sqlstate', None) or getattr(original, 'pgcode', None)
        if sqlstate in {'40001', '40P01', '55P03'}:
            return True
    return status in {408, 425, 429} or status >= 500


def now():
    return datetime.now(timezone.utc)


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def digest(value):
    return hashlib.sha256(json.dumps(jsonable_encoder(value), sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _cipher():
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(
        ('workflow-response-v1:' + settings.SECRET_KEY).encode()).digest()))


def _pack(response):
    if isinstance(response, JSONResponse):
        body, status = json.loads(response.body), response.status_code
    else:
        body, status = jsonable_encoder(response), 200
    return {'encrypted': _cipher().encrypt(json.dumps({'body': body, 'status': status}).encode()).decode()}


def _unpack(value):
    result = json.loads(_cipher().decrypt(value['encrypted'].encode()))
    return JSONResponse(status_code=result['status'], content=result['body'])


def public_response(kind, response):
    """Use the same caller-facing projection for POST replay and result reads.

    Stored review responses retain the capability for the separately authorized
    agent retrieval service; every reviewer-facing response removes it.
    """
    if kind != 'review':
        return response
    body = json.loads(response.body)
    if isinstance(body, dict):
        body['capability_token'] = None
        if isinstance(body.get('evaluation'), dict):
            body['evaluation']['capability_token'] = None
    return JSONResponse(status_code=response.status_code, content=body,
        headers={key: value for key, value in response.headers.items() if key.lower() != 'content-length'})


def _save(session, row):
    valid = WorkflowWrite.model_validate(row.model_dump())
    for key, value in valid.model_dump(mode='python').items():
        setattr(row, key, value.value if isinstance(value, WorkflowStatus) else value)
    session.add(row)
    session.flush()


class Operation:
    def __init__(self, engine, operation_id, owner, authorization=None, generation=1,
                 lost_event=None):
        self.engine, self.operation_id, self.owner = engine, operation_id, owner
        self.authorization = authorization
        self.generation = generation
        self.lost_event = lost_event
        self.checkpoint = {}

    def transaction(self, stage, work, *, complete=False, failure=None):
        """Callback is database-only. Its writes and checkpoint commit together."""
        if self.lost_event is not None and self.lost_event.is_set():
            raise OperationLeaseExpired()
        with Session(self.engine, expire_on_commit=False) as session:
            row = session.exec(select(WorkflowOperation).where(
                WorkflowOperation.operation_id == self.operation_id).with_for_update()).one()
            if (row.owner != self.owner or row.generation != self.generation
                    or row.status != WorkflowStatus.PROCESSING.value
                    or aware(row.lease_until) <= now()
                    or (row.run_deadline is not None and aware(row.run_deadline) <= now())):
                raise OperationLeaseExpired()
            valid_until = min(aware(row.lease_until), aware(row.run_deadline)) if row.run_deadline else aware(row.lease_until)
            if self.authorization is not None:
                from src.services import authentication as auth
                caller, kind, scope, payload = self.authorization
                if row.caller_principal_id != caller.principal_id:
                    raise HTTPException(404, 'operation not found')
                verified = auth.authorize_operation(session, caller, kind, scope, payload)
            if self.lost_event is not None and self.lost_event.is_set():
                raise OperationLeaseExpired()
            self.checkpoint = deepcopy(row.checkpoint)
            result = work(session, self.checkpoint)
            if now() >= valid_until:
                raise OperationLeaseExpired()
            row.stage, row.updated_at = stage, now()
            renewed = now() + timedelta(seconds=settings.WORKFLOW_LEASE_SECONDS)
            row.lease_until = min(renewed, aware(row.run_deadline)) if row.run_deadline else renewed
            row.heartbeat_at = now()
            row.checkpoint = deepcopy(self.checkpoint)
            if complete:
                row.status, row.response = WorkflowStatus.COMPLETED.value, _pack(result)
                row.owner = row.lease_until = row.run_deadline = row.heartbeat_at = None
                row.queue_deadline = row.next_run_at = None
            elif failure is not None:
                if row.execution_mode == 'background':
                    from src.services.workflow_dispatch import fail_background
                    fail_background(session, row, failure, retryable=True, reason='parser_provider_exhausted')
                else:
                    row.status, row.error, row.retryable = WorkflowStatus.FAILED.value, failure, True
            _save(session, row)
            if self.authorization is not None:
                auth.event(session, verified, 'accepted', 'workflow:' + stage)
            if now() >= valid_until or (self.lost_event is not None and self.lost_event.is_set()):
                raise OperationLeaseExpired()
            from src.services import monitoring
            with monitoring.span("commit"):
                session.commit()
            monitoring.commit_pending_link(self.engine, self.operation_id)
            monitoring.record('workflow', 'failed' if failure else 'completed' if complete else 'processing',
                              parser_trace={'operation_id': self.operation_id, 'stage': stage})
            monitoring.flush_current(self.engine)
            return result

    def slow(self, stage, work):
        self.transaction(stage, lambda session, checkpoint: None)
        if self.lost_event is not None and self.lost_event.is_set():
            raise OperationLeaseExpired()
        assert_no_transaction()
        from src.services import monitoring
        monitoring.record('external_work', 'started',
                          parser_trace={'operation_id': self.operation_id, 'stage': stage})
        monitoring.flush_current(self.engine)
        try:
            result = work()
        except Exception as exc:
            monitoring.record('external_work', 'failed', error_type=type(exc).__name__,
                              http_status=getattr(exc, 'status_code', None),
                              validation_errors=getattr(exc, 'validation_errors', None),
                              parser_trace={'operation_id': self.operation_id, 'stage': stage})
            monitoring.flush_current(self.engine)
            raise
        monitoring.record('external_work', 'completed',
                          parser_trace={'operation_id': self.operation_id, 'stage': stage})
        monitoring.flush_current(self.engine)
        return result

    def fail(self, exc, *, automatic_retry=False):
        with Session(self.engine) as session:
            row = session.get(WorkflowOperation, self.operation_id, with_for_update=True)
            if (row is None or row.owner != self.owner or row.generation != self.generation
                    or row.status != WorkflowStatus.PROCESSING.value):
                return
            from src.services.workflow_failure import failure_from_exception, close_interpretation
            failure = failure_from_exception(exc)
            retryable = _is_retryable(exc, failure['http_status'])
            if row.execution_mode == 'background':
                from src.services.workflow_dispatch import fail_background
                fail_background(session, row, failure, retryable=retryable,
                                automatic_retry=automatic_retry)
                session.commit()
                return
            row.status, row.error, row.retryable = WorkflowStatus.FAILED.value, failure, retryable
            row.updated_at = now()
            close_interpretation(session, row, failure)
            _save(session, row)
            session.commit()


def run(engine, kind, scope, key, payload, work):
    """Claim, resume, or return the existing operation without duplicate effects."""
    if not key or not key.strip() or len(key) > 200:
        raise HTTPException(422, 'idempotency key must contain 1 to 200 characters')
    from src.services import authentication as auth
    caller = auth.current.get()
    stored_scope = digest([caller.principal_id, scope]) if caller else scope
    operation_id, owner = digest([kind, stored_scope, key]), str(uuid4())
    try:
        with Session(engine, expire_on_commit=False) as session:
            row = session.get(WorkflowOperation, operation_id, with_for_update=True)
            if caller:
                auth.authorize_operation(session, caller, kind, scope, payload)
            if row is not None:
                if row.caller_principal_id != (caller.principal_id if caller else None):
                    raise HTTPException(404, 'operation not found')
                WorkflowWrite.model_validate(row.model_dump())
                if row.input_hash != digest(payload):
                    raise HTTPException(409, 'idempotency key was already used with different input')
                if row.status == WorkflowStatus.COMPLETED.value:
                    response = public_response(kind, _unpack(row.response))
                    if caller:
                        auth.event(session, caller, 'accepted', 'workflow:cached_response')
                        session.commit()
                    response.headers['X-Operation-ID'] = operation_id
                    return response
                if row.status == WorkflowStatus.FAILED.value and not row.retryable:
                    raise HTTPException(row.error['http_status'], row.error['detail'])
                if row.execution_mode == 'background':
                    # A flag change or concurrent acceptance must never turn a
                    # persisted background generation into synchronous work.
                    if row.status in {WorkflowStatus.QUEUED.value, WorkflowStatus.PROCESSING.value,
                                      WorkflowStatus.RETRY_WAIT.value}:
                        from src.services.workflow_submission import _accepted
                        if caller:
                            auth.event(session, caller, 'accepted', 'workflow:progress')
                            session.commit()
                        return _accepted(row)
                    raise RetryableWorkflowConflict('resume this background operation through its submission endpoint')
                if row.status == WorkflowStatus.PROCESSING.value and aware(row.lease_until) > now():
                    progress = WorkflowProgress(status='processing', operation_id=operation_id,
                        stage=row.stage, status_url=f'/api/v1/operations/{operation_id}',
                        result_url=f'/api/v1/operations/{operation_id}/result')
                    if caller:
                        auth.event(session, caller, 'accepted', 'workflow:progress')
                        session.commit()
                    return JSONResponse(status_code=202, headers={'Retry-After': '3', 'X-Operation-ID': operation_id},
                                        content=progress.model_dump(mode='json'))
                row.owner, row.status = owner, WorkflowStatus.PROCESSING.value
                row.error, row.retryable = None, False
                row.lease_until, row.updated_at = now() + timedelta(seconds=settings.WORKFLOW_LEASE_SECONDS), now()
            else:
                row = WorkflowOperation(operation_id=operation_id, kind=kind, scope=stored_scope,
                    caller_principal_id=caller.principal_id if caller else None,
                    idempotency_key=key, input_hash=digest(payload), status=WorkflowStatus.PROCESSING.value,
                    stage='claimed', owner=owner,
                    lease_until=now() + timedelta(seconds=settings.WORKFLOW_LEASE_SECONDS))
            _save(session, row)
            if caller:
                auth.event(session, caller, 'accepted', 'workflow:claimed')
            session.commit()
    except IntegrityError:
        # Simultaneous first claims collide on the identity key. Re-read the
        # committed winner instead of starting a second computation.
        with Session(engine) as session:
            if session.get(WorkflowOperation, operation_id) is None:
                raise
        return run(engine, kind, scope, key, payload, work)
    operation = Operation(engine, operation_id, owner,
                          (caller, kind, scope, payload) if caller else None,
                          generation=row.generation)
    try:
        result = work(operation)
        response = result if isinstance(result, JSONResponse) else _unpack(_pack(result))
        response.headers['X-Operation-ID'] = operation_id
        return public_response(kind, response)
    except Exception as exc:
        try:
            operation.fail(exc)
        except Exception:
            # Leave the durable claim recoverable by lease expiry if the DB is down.
            import logging
            logging.getLogger(__name__).exception('could not record workflow failure %s', operation_id)
        raise


def status(session, operation_id):
    row = session.get(WorkflowOperation, operation_id)
    if row is None:
        raise HTTPException(404, 'operation not found')
    error_summary = None
    if row.error:
        error_summary = {key: row.error[key] for key in ('error_type', 'http_status', 'detail')}
        if row.execution_mode == 'background':
            error_summary['detail'] = 'operation failed; retrieve the owner-authorized result'
    result = {'operation_id': row.operation_id, 'kind': row.kind, 'status': row.status,
            'stage': row.stage, 'retryable': row.retryable,
            'error': error_summary,
            'lease_expired': row.status == WorkflowStatus.PROCESSING.value and aware(row.lease_until) <= now(),
            'lease_until': row.lease_until, 'updated_at': row.updated_at,
            'created_at': row.created_at, 'next_run_at': row.next_run_at,
            'run_count': row.run_count, 'generation': row.generation,
            'result_url': f'/api/v1/operations/{row.operation_id}/result'}
    if row.execution_mode == 'background' and row.status in {WorkflowStatus.QUEUED.value, WorkflowStatus.RETRY_WAIT.value}:
        from src.models.workflow_dispatch import WorkflowDispatch
        dispatch = session.exec(select(WorkflowDispatch).where(
            WorkflowDispatch.operation_id == row.operation_id,
            WorkflowDispatch.generation == row.generation)).one_or_none()
        result['dispatch'] = {
            'status': dispatch.status if dispatch else None,
            'publish_attempts': dispatch.publish_attempts if dispatch else 0,
            'waiting_reason': ('broker_unavailable' if dispatch and dispatch.last_error else
                               'awaiting_dispatch' if dispatch and dispatch.status == 'PENDING' else
                               'awaiting_worker' if dispatch and dispatch.status == 'PUBLISHED' else 'publishing')}
    return result


def result(session, row):
    """Owner-only result projection; never decrypt progress or expose worker state."""
    from fastapi.responses import JSONResponse
    headers = {'Cache-Control': 'no-store', 'X-Operation-ID': row.operation_id,
               'X-Operation-Status': row.status}
    if row.status == WorkflowStatus.COMPLETED.value:
        response = public_response(row.kind, _unpack(row.response))
        response.headers.update(headers)
        return response
    if row.status == WorkflowStatus.FAILED.value:
        # Parser/provider failures can have a complete, intentionally recorded
        # response checkpoint even though the operation's state is FAILED.
        frozen = (row.checkpoint.get('result') if row.checkpoint
                  and row.checkpoint.get('result_final') else None)
        if frozen:
            response = public_response(row.kind, _unpack(frozen))
            response.headers.update(headers)
            return response
        body = {'detail': row.error['detail'] if row.error else 'operation failed'}
        return JSONResponse(status_code=row.error['http_status'] if row.error else 503,
                            content=body, headers=headers)
    progress = {'status': row.status.lower(), 'operation_id': row.operation_id,
                'stage': row.stage, 'status_url': f'/api/v1/operations/{row.operation_id}',
                'result_url': f'/api/v1/operations/{row.operation_id}/result'}
    return JSONResponse(status_code=202, content=progress, headers={**headers, 'Retry-After': '3'})
