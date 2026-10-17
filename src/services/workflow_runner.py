"""Allowlisted, process-independent entry point for background workflows."""
from datetime import datetime
from src.db.database import engine
from src.models.request import (ActionRequest, NaturalLanguageRequest, ClarificationAnswer)
from src.services import authentication as auth, monitoring, request_parsing, workflow_flows
from src.services.workflow import Operation
from src.services.workflow_submission import open_input


def caller_from_input(data):
    identity = data['caller']
    return auth.Caller(
        principal_id=identity['principal_id'], kind=identity['kind'], agent_id=identity['agent_id'],
        actor_role=identity['actor_role'], permissions=frozenset(), auth_version=identity['auth_version'],
        credential_id=identity['credential_id'],
        expires_at=datetime.fromisoformat(identity['expires_at']) if identity['expires_at'] else None,
        correlation_id=identity['correlation_id'], route=identity['route'])


def validate_input(data, row=None):
    """Validate the encrypted task contract before execution or ownership claim."""
    from src.models.workflow_input import BackgroundInput
    from src.services.workflow import digest
    BackgroundInput.model_validate(data)
    if row is not None:
        scope = digest([data['caller']['principal_id'], data['scope']])
        if (row.kind != data['kind'] or row.scope != scope or row.idempotency_key != data['key']
                or row.input_hash != digest(data['payload'])
                or row.caller_principal_id != data['caller']['principal_id']
                or row.operation_id != digest([data['kind'], scope, data['key']])):
            raise ValueError('encrypted workflow input differs from its persisted identity')


def run_operation(row, owner, lost_event=None):
    data = open_input(row.input_envelope)
    validate_input(data, row)
    caller = caller_from_input(data)
    context = auth.current.set(caller)
    attempt = monitoring.Attempt(route=caller.route,
                                 operation_id=row.operation_id,
                                 correlation_id=caller.correlation_id,
                                 persistence_engine=engine)
    monitor_context = monitoring._current.set(attempt)
    try:
        packed_result = (row.checkpoint or {}).get('result')
        if packed_result:
            from src.services.workflow import _unpack
            import json
            stored_body = json.loads(_unpack(packed_result).body)
            def find_decision(value):
                if isinstance(value, dict):
                    direct = value.get('decision_id')
                    return direct or next((found for child in value.values()
                                           if (found := find_decision(child))), None)
                if isinstance(value, list):
                    return next((found for child in value if (found := find_decision(child))), None)
                return None
            existing_decision = find_decision(stored_body)
            if existing_decision:
                monitoring.link_decision(existing_decision)
        op = Operation(engine, row.operation_id, owner,
                       (caller, data['kind'], data['scope'], data['payload']),
                       generation=row.generation, lost_event=lost_event)
        payload = data['payload']
        if data['kind'] == 'evaluate':
            result = workflow_flows.evaluate(op, ActionRequest.model_validate(data['normalized_request']))
        elif data['kind'] == 'interpret':
            result = workflow_flows.interpret(op, NaturalLanguageRequest.model_validate(data['normalized_request']),
                                              request_parsing.parse_for_evaluation)
        elif data['kind'] == 'clarify':
            answer = ClarificationAnswer.model_validate(payload)
            source = NaturalLanguageRequest.model_validate(data['normalized_request'])
            result = workflow_flows.interpret(op, source, request_parsing.parse_for_evaluation,
                                              interpretation_id=data['scope'], answer=answer)
        elif data['kind'] == 'rollback':
            result = workflow_flows.rollback(op, data['scope'])
        elif data['kind'] == 'prepare_review':
            result = workflow_flows.prepare(op, data['scope'])
        else:
            raise ValueError('unsupported background workflow kind')
        return result, attempt, op
    except Exception as exc:
        setattr(exc, '_monitoring_attempt', attempt)
        raise
    finally:
        auth.current.reset(context)
        monitoring._current.reset(monitor_context)
