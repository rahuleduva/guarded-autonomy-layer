"""Stage application workflows using detached data across slow boundaries."""
from copy import deepcopy
from datetime import datetime, timezone
import json

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlmodel import select

from src.models.clarification import InterpretationStatus, RequestInterpretation, InterpretationWrite
from src.models.request import ActionRequest, NaturalLanguageRequest, EscalationPackage, RollbackResult
from src.services import clarification_service, escalation_service, executor, llm_parser, monitoring
from src.services.advisory_evidence import build_evidence
from src.services.orchestrator import evaluate_action
from src.services.workflow import RetryableWorkflowConflict


def _checkpoint(op, stage, key, value):
    op.transaction(stage, lambda session, checkpoint: checkpoint.update({key: jsonable_encoder(value)}))


def _response(value):
    from src.services.workflow import _pack
    return _pack(value)


def _body(value):
    from src.services.workflow import _unpack
    return json.loads(_unpack(value).body)


def _finish(op):
    from src.services.workflow import _unpack
    def finish(session, checkpoint):
        checkpoint['result_final'] = True
        return _unpack(checkpoint['result'])
    return op.transaction('completed', finish, complete=True)


def _prepare(op, decision_id):
    if op.checkpoint.get('package_ready'):
        return
    context = op.transaction('preparing_review', lambda session, checkpoint:
                             escalation_service.preparation_context(session, decision_id))
    if context['package'].get('package_fingerprint'):
        prepared = EscalationPackage.model_validate(context['package'])
    else:
        prepared = op.slow('explaining_review', lambda: escalation_service.prepare_package(context))
    def save(session, checkpoint):
        escalation_service.prepare_existing(session, decision_id, prepared)
        checkpoint['package_ready'] = True
    op.transaction('review_ready', save)


def _evidence(op, action):
    if 'evidence' not in op.checkpoint:
        evidence = op.slow('semantic_evidence', lambda: build_evidence(action))
        _checkpoint(op, 'evidence_ready', 'evidence', evidence)
    return deepcopy(op.checkpoint['evidence'])


def evaluate(op, request):
    op.transaction('input_ready', lambda session, checkpoint:
                   checkpoint.setdefault('action', request.model_dump(mode='json')))
    action = ActionRequest.model_validate(op.checkpoint['action'])
    if 'result' not in op.checkpoint:
        evidence = _evidence(op, action)
        def finalize(session, checkpoint):
            result = evaluate_action(session, action, evidence=evidence)
            checkpoint['result'] = _response(result)
        op.transaction('decision_recorded', finalize)
    body = _body(op.checkpoint['result'])
    if body.get('decision') in {'ESCALATED', 'PENDING_APPROVAL'}:
        _prepare(op, body['decision_id'])
    return _finish(op)


def _reserve(session, checkpoint, request, interpretation_id=None, answer=None):
    def reject(status, message):
        monitoring.record('clarification_rejected', 'rejected', http_status=status,
            validation_errors={'non_field_errors': [{'code': f'HTTP_{status}', 'message': message}]},
            parser_trace={'interpretation_id': interpretation_id, 'answer': jsonable_encoder(answer)})
        raise HTTPException(status, message)
    if 'interpretation_id' in checkpoint:
        # Resume the exact frozen turn, or append a turn after a recorded
        # failure. A newer clarification must never be reopened by this job.
        from types import SimpleNamespace
        interpretation_id = checkpoint['interpretation_id']
        request = NaturalLanguageRequest.model_validate(checkpoint['source'])
        answer = SimpleNamespace(agent_id=request.agent_id, actor_role=request.actor_role,
                                 revision=checkpoint['revision'])
        existing = session.get(RequestInterpretation, interpretation_id,
                               with_for_update=True, populate_existing=True)
        if (existing is None or existing.revision != checkpoint['revision'] or not existing.turns
                or existing.turns[-1].get('request_id') != request.request_id):
            reject(409, 'interpretation reservation was superseded; submit a current clarification')
        clarification_service.validate_history(session, existing)
        if (existing.agent_id, existing.actor_role) != (request.agent_id, request.actor_role):
            reject(403, 'caller identity differs from the original request')
        if not checkpoint.get('provider_retry_pending'):
            if existing.status == InterpretationStatus.PROCESSING.value:
                return
            if existing.status != InterpretationStatus.PROCESSING_FAILED.value:
                if 'result' in checkpoint:
                    return
                reject(409, 'interpretation is already closed')
    if interpretation_id is None:
        row = RequestInterpretation(agent_id=request.agent_id, actor_role=request.actor_role,
                                    original_prompt=request.raw_prompt,
                                    status=InterpretationStatus.PROCESSING.value)
    else:
        row = session.exec(select(RequestInterpretation).where(
            RequestInterpretation.interpretation_id == interpretation_id).with_for_update()).one_or_none()
        if row is None:
            reject(404, 'clarification request not found')
        clarification_service.validate_history(session, row)
        if (row.agent_id, row.actor_role) != (answer.agent_id, answer.actor_role):
            reject(403, 'caller identity differs from the original request')
        if row.status == InterpretationStatus.PROCESSING.value:
            raise RetryableWorkflowConflict(
                'interpretation is still processing; retry with the same idempotency key'
            )
        if row.status not in {InterpretationStatus.NEEDS_CLARIFICATION.value, InterpretationStatus.PROVIDER_FAILED.value, InterpretationStatus.PROCESSING_FAILED.value}:
            reject(409, 'interpretation is already closed')
        if row.revision != answer.revision:
            reject(409, 'stale clarification revision')
        row.revision += 1
        row.status = InterpretationStatus.PROCESSING.value
    row.questions, row.decision_id = [], None
    row.updated_at = datetime.now(timezone.utc)
    row.turns = [*row.turns, dict(revision=row.revision, request_id=request.request_id,
        prompt=request.raw_prompt, status=InterpretationStatus.PROCESSING.value,
        submitted_at=row.updated_at.isoformat(), correlation_id=monitoring.current_attempt_id(), questions=[])]
    valid = InterpretationWrite.model_validate(row.model_dump())
    row.turns = valid.model_dump(mode='json')['turns']
    session.add(row)
    session.flush()
    checkpoint.update(interpretation_id=row.interpretation_id, revision=row.revision,
                      source=request.model_dump(mode='json'))
    if checkpoint.pop('provider_retry_pending', False):
        checkpoint.pop('parsed', None)
        checkpoint.pop('result', None)


def _parse(source, parse):
    try:
        action = parse(source)
        return {'action': action.model_dump(mode='json'), 'analysis': action._semantic_analysis}
    except (llm_parser.OnlineActionParseError, llm_parser.ParserRejectionError) as exc:
        from src.services.parser_workflow import _validation_errors
        root = exc
        seen = set()
        while root.__cause__ is not None and id(root) not in seen:
            seen.add(id(root))
            root = root.__cause__
        attributes = {name: getattr(exc, name, None) for name in (
            'analysis', 'parser_attempts', 'parser_feedback', 'questions')}
        attributes['validation_errors'] = _validation_errors(exc if getattr(exc, 'validation_errors', None) else root)
        return dict(error_type=type(exc).__name__, message=str(exc), attributes=attributes)


def _parsed_callback(parsed):
    def callback(source):
        if 'action' in parsed:
            action = ActionRequest.model_validate(parsed['action'])
            action._semantic_analysis = parsed.get('analysis')
            return action
        if parsed['error_type'] == 'ClarificationRequired':
            errors = parsed['attributes']['validation_errors']
            field = next(iter(errors))
            issue = errors[field][0]
            exc = llm_parser.ClarificationRequired(field, issue['code'], issue['message'])
        else:
            cls = getattr(llm_parser, parsed['error_type'], llm_parser.ParserRejectionError)
            exc = cls(parsed['message'])
        for name, value in parsed['attributes'].items():
            if value is not None:
                setattr(exc, name, value)
        raise exc
    return callback


def interpret(op, request, parse, *, interpretation_id=None, answer=None):
    op.transaction('parser_reserved', lambda session, checkpoint:
                   _reserve(session, checkpoint, request, interpretation_id, answer))
    source = NaturalLanguageRequest.model_validate(op.checkpoint['source'])
    if 'parsed' not in op.checkpoint:
        parsed = op.slow('parsing', lambda: _parse(source, parse))
        _checkpoint(op, 'parsed', 'parsed', parsed)
    parsed = op.checkpoint['parsed']
    if 'result' not in op.checkpoint:
        evidence = None
        if 'action' in parsed:
            action = _parsed_callback(parsed)(source)
            evidence = _evidence(op, action)
        unavailable = parsed.get('error_type') == 'OnlineActionParseError'
        def finalize(session, checkpoint):
            row = session.get(RequestInterpretation, checkpoint['interpretation_id'], with_for_update=True)
            if row.status != InterpretationStatus.PROCESSING.value or row.revision != checkpoint['revision']:
                raise HTTPException(409, 'interpretation reservation changed')
            result = clarification_service.submit(session, source, _parsed_callback(parsed), row=row,
                finalize_action=lambda session, action: evaluate_action(session, action, evidence=evidence),
                parser_is_frozen=True)
            checkpoint['result'] = _response(result)
            if unavailable:
                checkpoint['provider_retry_pending'] = True
                checkpoint['result_final'] = True
            return result
        response = op.transaction('provider_failed' if unavailable else 'interpretation_recorded', finalize,
            failure={'error_type': parsed['error_type'], 'http_status': 502,
                     'detail': parsed['message']} if unavailable else None)
        if unavailable:
            return response
    body = _body(op.checkpoint['result'])
    if body.get('decision') in {'ESCALATED', 'PENDING_APPROVAL'}:
        _prepare(op, body['decision_id'])
    return _finish(op)


def prepare(op, decision_id):
    op.transaction('input_ready', lambda session, checkpoint: None)
    _prepare(op, decision_id)
    def finish(session, checkpoint):
        context = escalation_service.preparation_context(session, decision_id)
        return EscalationPackage.model_validate(context['package'])
    return op.transaction('completed', finish, complete=True)


def rollback(op, execution_id):
    if not op.checkpoint:
        def snapshot(session, checkpoint):
            if 'rollback' not in checkpoint:
                checkpoint['rollback'] = executor.rollback_context(session, execution_id)
        op.transaction('rollback_snapshot', snapshot)
    context = op.checkpoint['rollback']
    if context is None:
        return op.transaction('completed', lambda session, checkpoint:
            RollbackResult(execution_id=execution_id, status='FAILED', restored_state={}), complete=True)
    if 'result' not in op.checkpoint:
        action = ActionRequest.model_validate(context['request'])
        evidence = _evidence(op, action)
        def finalize(session, checkpoint):
            result, decision_id = executor.finalize_rollback(session, execution_id, context, evidence)
            checkpoint['result'] = _response(result)
            checkpoint['compensation_decision_id'] = decision_id
        op.transaction('rollback_recorded', finalize)
    if op.checkpoint.get('compensation_decision_id'):
        _prepare(op, op.checkpoint['compensation_decision_id'])
    return _finish(op)
