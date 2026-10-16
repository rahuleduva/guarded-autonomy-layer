"""Persist caller clarification separately from policy approval and execution."""
from datetime import datetime, timezone
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from sqlmodel import select
from src.models.clarification import RequestInterpretation, InterpretationStatus, InterpretationWrite
from src.models.request import (NaturalLanguageRequest, ClarificationAnswer, ClarificationResponse,
                                ParserRejectionResponse, ParserFailureResponse)
from src.models.decision_contracts import ParserDecisionEvidence
from src.models.decision import DecisionRecord
from src.services import llm_parser, monitoring
from src.services.llm_routing import exception_chain as _exception_chain
from src.services.parser_workflow import _validation_errors
from src.services.orchestrator import evaluate_action
from src.services.decision_ledger import log_parser_rejection


def validate_history(session, row):
    validated = InterpretationWrite.model_validate(row.model_dump())
    if validated.decision_id is not None:
        decision = session.get(DecisionRecord, validated.decision_id)
        latest = validated.turns[-1]
        if decision is None or (
            decision.decision_stage, decision.request_id, decision.agent_id, decision.actor_role,
        ) != (
            latest.decision_stage, latest.request_id, validated.agent_id, validated.actor_role,
        ):
            raise ValueError('interpretation history does not match its linked ledger decision')
    return validated


def resume(session, interpretation_id: str, answer: ClarificationAnswer, evaluate):
    answer = ClarificationAnswer.model_validate(answer)
    def reject(status, message):
        monitoring.record('clarification_rejected', 'rejected', http_status=status,
                          validation_errors={'non_field_errors': [{'code': f'HTTP_{status}', 'message': message}]},
                          parser_trace={'interpretation_id': interpretation_id,
                                        'answer': answer.model_dump(mode='json')})
        raise HTTPException(status, message)
    # Serialize updates and reject duplicate/stale answers before any model call.
    row = session.exec(select(RequestInterpretation).where(
        RequestInterpretation.interpretation_id == interpretation_id).with_for_update()).first()
    if row is None:
        reject(404, 'clarification request not found')
    validate_history(session, row)
    if (row.agent_id, row.actor_role) != (answer.agent_id, answer.actor_role):
        reject(403, 'caller identity differs from the original request')
    if row.status not in {InterpretationStatus.NEEDS_CLARIFICATION, InterpretationStatus.PROVIDER_FAILED}:
        reject(409, 'this interpretation is already closed')
    if row.revision != answer.revision:
        reject(409, 'stale clarification revision')
    row.revision += 1
    return submit(session, NaturalLanguageRequest(
        agent_id=row.agent_id, actor_role=row.actor_role, raw_prompt=answer.clarified_prompt,
    ), evaluate, row=row)


def submit(session, request: NaturalLanguageRequest, evaluate, *, row=None):
    request = NaturalLanguageRequest.model_validate(request)
    if row is None:
        row = RequestInterpretation(agent_id=request.agent_id, actor_role=request.actor_role,
                                    original_prompt=request.raw_prompt,
                                    status=InterpretationStatus.NEEDS_CLARIFICATION)
    turn = {'revision': row.revision, 'request_id': request.request_id, 'prompt': request.raw_prompt,
            'correlation_id': monitoring.current_attempt_id(),
            'submitted_at': datetime.now(timezone.utc).isoformat()}
    response = None
    try:
        parsed = evaluate(request)
        for field in ('request_id', 'agent_id', 'actor_role'):
            if getattr(parsed, field) != getattr(request, field):
                raise llm_parser.UnverifiedActionError(f'parsed {field} differs from the submitted request')
    except llm_parser.ClarificationRequired as exc:
        row.status = InterpretationStatus.NEEDS_CLARIFICATION
        row.questions = exc.questions
        turn.update(status=row.status, validation_errors=exc.validation_errors,
                    error_message=str(exc), analysis=getattr(exc, 'analysis', None))
        response = JSONResponse(status_code=202, content=ClarificationResponse(
            request_id=request.request_id,
            interpretation_id=row.interpretation_id, revision=row.revision,
            questions=row.questions, validation_errors=exc.validation_errors,
        ).model_dump(mode='json'))
    except (llm_parser.OnlineActionParseError, llm_parser.ParserRejectionError) as exc:
        root = exc
        seen = set()
        while root.__cause__ is not None and id(root) not in seen:
            seen.add(id(root))
            root = root.__cause__
        unavailable = isinstance(exc, llm_parser.OnlineActionParseError)
        row.status = (InterpretationStatus.PROVIDER_FAILED if unavailable
                      else InterpretationStatus.REJECTED)
        row.questions = []
        turn.update(status=row.status, error_type=type(exc).__name__, error_message=str(exc),
                    validation_errors=_validation_errors(exc) if getattr(exc, 'validation_errors', None)
                    else _validation_errors(root), analysis=getattr(exc, 'analysis', None),
                    parser_attempts=getattr(exc, 'parser_attempts', None),
                    correction_message=getattr(exc, 'parser_feedback', None) or getattr(root, 'parser_feedback', None))
        response_fields = dict(request_id=request.request_id, interpretation_id=row.interpretation_id,
                               revision=row.revision, detail=str(exc), validation_errors=turn['validation_errors'])
        if unavailable:
            payload = ParserFailureResponse(**response_fields)
        else:
            rejection_evidence = ParserDecisionEvidence(
                interpretation_id=row.interpretation_id, correlation_id=turn['correlation_id'],
                error_type=turn['error_type'], error_message=turn['error_message'],
                validation_errors=turn['validation_errors'], analysis=turn['analysis'],
                parser_attempts=turn['parser_attempts'], correction_message=turn['correction_message'],
            )
            decision = log_parser_rejection(session, request, str(exc), rejection_evidence)
            row.decision_id = decision.decision_id
            turn.update(decision_id=decision.decision_id, decision_stage=decision.decision_stage)
            payload = ParserRejectionResponse(decision_id=decision.decision_id, **response_fields)
        response = JSONResponse(status_code=502 if unavailable else 400, content=payload.model_dump(mode='json'))
    except Exception as exc:
        # The transaction handler maps bare ValueError to HTTP 400. Unexpected
        # parser/configuration failures must instead abort as server errors.
        chain = list(_exception_chain(exc))
        monitoring.record('parser_internal_failure', 'failed', error_type=type(exc).__name__,
                          validation_errors=_validation_errors(chain[-1]),
                          parser_trace={'interpretation_id': row.interpretation_id, **turn,
                                        'error_message': str(exc),
                                        'error_chain': [{'error_type': type(error).__name__,
                                                         'message': str(error)} for error in chain]})
        raise RuntimeError('request interpretation could not be completed') from exc
    else:
        # Policy failures must roll back; they are never parser rejections.
        response = evaluate_action(session, parsed)
        response.interpretation_id = row.interpretation_id
        row.status = InterpretationStatus.EVALUATED
        row.questions = []
        row.decision_id = response.decision_id
        turn.update(status=row.status, decision_id=response.decision_id, decision_stage=response.decision_stage,
                    request_id=response.request_id,
                    action_request=response.action_request.model_dump(mode='json'))
    turn['status'] = str(row.status.value if isinstance(row.status, InterpretationStatus) else row.status)
    turn['questions'] = list(row.questions)
    row.turns = [*row.turns, turn]
    row.updated_at = datetime.now(timezone.utc)
    validated = validate_history(session, row)
    row.turns = validated.model_dump(mode='json')['turns']
    session.add(row)
    monitoring.record('interpretation', turn['status'], decision_id=row.decision_id,
                      validation_errors=turn.get('validation_errors'),
                      parser_trace={'interpretation_id': row.interpretation_id, **turn,
                                    'questions': row.questions})
    return response
