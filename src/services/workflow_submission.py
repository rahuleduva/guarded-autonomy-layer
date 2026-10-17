"""Idempotent API submission into the PostgreSQL transactional outbox."""
import base64
import hashlib
import json
from copy import deepcopy
from datetime import timedelta

from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from src.config import settings
from src.models.workflow import WorkflowOperation, WorkflowProgress, WorkflowStatus, WorkflowWrite
from src.models.workflow_dispatch import DispatchStatus, WorkflowDispatch, save_dispatch
from src.models.workflow_input import BackgroundInput
from src.models.request import ActionRequest, NaturalLanguageRequest, ClarificationAnswer
from src.services import authentication as auth
from src.services.workflow import digest, now, _unpack, _save, public_response


ALLOWED_BACKGROUND_KINDS = {'evaluate', 'interpret', 'clarify', 'rollback', 'prepare_review'}


def _input_cipher():
    key = hashlib.sha256(('workflow-input-v1:' + settings.SECRET_KEY).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _seal(value):
    return {'encrypted': _input_cipher().encrypt(json.dumps(jsonable_encoder(value),
            separators=(',', ':'), sort_keys=True).encode()).decode()}


def open_input(value):
    data = json.loads(_input_cipher().decrypt(value['encrypted'].encode()))
    return BackgroundInput.model_validate(data).model_dump(mode='json')


def _envelope(caller, kind, scope, key, payload, normalized_request):
    value = BackgroundInput.model_validate({'version': 'background-input-v1',
        'kind': kind, 'scope': scope, 'key': key,
        'payload': jsonable_encoder(payload), 'normalized_request': normalized_request, 'caller': {
            'principal_id': caller.principal_id, 'kind': caller.kind,
            'agent_id': caller.agent_id, 'actor_role': caller.actor_role,
            'auth_version': caller.auth_version, 'credential_id': caller.credential_id,
            'expires_at': caller.expires_at.isoformat() if caller.expires_at else None,
            'correlation_id': caller.correlation_id, 'route': caller.route}})
    return _seal(value.model_dump(mode='json'))


def _normalize(kind, payload, normalized_request):
    if kind == 'evaluate':
        return ActionRequest.model_validate(normalized_request or payload).model_dump(mode='json')
    if kind == 'interpret':
        return NaturalLanguageRequest.model_validate(normalized_request or payload).model_dump(mode='json')
    if kind == 'clarify':
        answer = ClarificationAnswer.model_validate(payload)
        source = normalized_request or dict(agent_id=answer.agent_id, actor_role=answer.actor_role,
                                             raw_prompt=answer.clarified_prompt)
        return NaturalLanguageRequest.model_validate(source).model_dump(mode='json')
    return None


def _reserve_input(session, row, data):
    if row.kind not in {'interpret', 'clarify'}:
        return
    from src.services.workflow_flows import _reserve
    checkpoint = deepcopy(row.checkpoint)
    source = NaturalLanguageRequest.model_validate(data['normalized_request'])
    answer = ClarificationAnswer.model_validate(data['payload']) if row.kind == 'clarify' else None
    _reserve(session, checkpoint, source,
             interpretation_id=data['scope'] if answer else None, answer=answer)
    row.checkpoint = checkpoint


def _accepted(row):
    body = WorkflowProgress(status=row.status.lower(), operation_id=row.operation_id,
        stage=row.stage, status_url=f'/api/v1/operations/{row.operation_id}',
        result_url=f'/api/v1/operations/{row.operation_id}/result').model_dump(mode='json')
    return JSONResponse(status_code=202, content=body,
                        headers={'Retry-After': '3', 'Location': f'/api/v1/operations/{row.operation_id}',
                                 'X-Operation-ID': row.operation_id,
                                 'Cache-Control': 'no-store'})


def _submit_once(engine, kind, scope, key, payload, normalized_request=None, *, allow_new=True):
    """Return None only for a legacy sync operation; otherwise accept/replay it."""
    if kind not in ALLOWED_BACKGROUND_KINDS:
        raise ValueError('operation kind is not eligible for background execution')
    if not key or not key.strip() or len(key) > 200:
        raise HTTPException(422, 'idempotency key must contain 1 to 200 characters')
    caller = auth.current.get()
    if caller is None:
        raise HTTPException(401, 'authentication required')
    stored_scope = digest([caller.principal_id, scope])
    operation_id = digest([kind, stored_scope, key])
    current_time = now()
    with Session(engine, expire_on_commit=False) as session:
        row = session.get(WorkflowOperation, operation_id, with_for_update=True)
        if row is None and not allow_new:
            return None
        auth.authorize_operation(session, caller, kind, scope, payload)
        if row is not None:
            if row.caller_principal_id != caller.principal_id:
                raise HTTPException(404, 'operation not found')
            WorkflowWrite.model_validate(row.model_dump())
            if row.input_hash != digest(payload):
                raise HTTPException(409, 'idempotency key was already used with different input')
            if row.execution_mode != 'background':
                # A pre-rollout synchronous operation keeps its old execution path.
                return None
            if row.status == WorkflowStatus.COMPLETED.value:
                response = public_response(kind, _unpack(row.response))
                auth.event(session, caller, 'accepted', 'workflow:cached_response')
                session.commit()
                response.headers['X-Operation-ID'] = operation_id
                response.headers['Cache-Control'] = 'no-store'
                return response
            if row.status == WorkflowStatus.FAILED.value and not row.retryable:
                raise HTTPException(row.error['http_status'], row.error['detail'])
            if row.status in {WorkflowStatus.QUEUED.value, WorkflowStatus.RETRY_WAIT.value}:
                auth.event(session, caller, 'accepted', 'workflow:queued')
                session.commit()
                return _accepted(row)
            if row.status == WorkflowStatus.PROCESSING.value:
                auth.event(session, caller, 'accepted', 'workflow:progress')
                session.commit()
                return _accepted(row)
            if row.status == WorkflowStatus.FAILED.value and row.retryable:
                frozen = open_input(row.input_envelope)
                row.generation += 1
                row.status, row.stage, row.owner = WorkflowStatus.QUEUED.value, 'queued', None
                row.lease_until = row.run_deadline = row.heartbeat_at = None
                row.next_run_at = None
                row.queue_deadline = current_time + timedelta(seconds=settings.WORKFLOW_QUEUE_TIMEOUT_SECONDS)
                row.error, row.retryable = None, False
                # An explicit retry may come with a renewed valid credential.
                # Preserve the exact input while refreshing trusted caller data.
                row.input_envelope = _envelope(caller, kind, scope, key,
                    frozen['payload'], frozen['normalized_request'])
                row.automatic_retry_count = 0
                row.updated_at = current_time
                _reserve_input(session, row, open_input(row.input_envelope))
                dispatch = WorkflowDispatch(dispatch_id=digest([operation_id, row.generation]),
                    operation_id=operation_id, generation=row.generation,
                    status=DispatchStatus.PENDING.value, available_at=current_time)
            else:
                raise HTTPException(409, 'operation state is not safely retryable')
        else:
            normalized = _normalize(kind, payload, normalized_request)
            envelope = _envelope(caller, kind, scope, key, payload, normalized)
            row = WorkflowOperation(operation_id=operation_id, kind=kind, scope=stored_scope,
                caller_principal_id=caller.principal_id, idempotency_key=key, input_hash=digest(payload),
                status=WorkflowStatus.QUEUED.value, stage='queued', owner=None, lease_until=None,
                execution_mode='background', input_envelope=envelope, generation=1,
                run_count=0, automatic_retry_count=0, next_run_at=None,
                queue_deadline=current_time + timedelta(seconds=settings.WORKFLOW_QUEUE_TIMEOUT_SECONDS),
                checkpoint={}, response=None, error=None, retryable=False)
            # Establish the unique operation identity before reserving a turn.
            # A concurrent identical clarification must hit the identity conflict
            # and replay its winner, rather than reject its processing turn.
            # Reservation and dispatch still commit atomically with this insert.
            _save(session, row)
            _reserve_input(session, row, open_input(envelope))
            dispatch = WorkflowDispatch(dispatch_id=digest([operation_id, 1]), operation_id=operation_id,
                generation=1, status=DispatchStatus.PENDING.value, available_at=current_time)
        _save(session, row)
        save_dispatch(session, dispatch)
        auth.event(session, caller, 'accepted', 'workflow:queued')
        session.commit()
    return _accepted(row)


def submit(engine, kind, scope, key, payload, normalized_request=None, *, allow_new=True):
    try:
        return _submit_once(engine, kind, scope, key, payload, normalized_request, allow_new=allow_new)
    except IntegrityError:
        # Concurrent first submissions race on the unique operation identity.
        # Re-read the committed winner and take the normal idempotent path.
        caller = auth.current.get()
        stored_scope = digest([caller.principal_id, scope]) if caller else scope
        operation_id = digest([kind, stored_scope, key])
        with Session(engine) as session:
            winner_exists = session.get(WorkflowOperation, operation_id) is not None
        if winner_exists:
            return _submit_once(engine, kind, scope, key, payload, normalized_request, allow_new=allow_new)
        raise HTTPException(409, 'duplicate request or conflicting workflow state') from None
