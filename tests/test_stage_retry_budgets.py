"""Provider outages cannot spend or replenish output-correction attempts."""
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, Field, ValidationError

from src.services import parser_workflow, semantic_failures
from src.services.llm_routing import FallbackPolicy, can_fallback, provider_failure_category


class Answer(BaseModel):
    number: int = Field(ge=1)


MESSAGES = [{'role': 'system', 'content': 'Return the answer.'},
            {'role': 'user', 'content': 'Use number 1.'}]


def run(parser, **kwargs):
    return parser_workflow.run_parser_workflow(
        parser=parser, messages=MESSAGES,
        parse_value=lambda value: (Answer.model_validate(value), None),
        validate_action=lambda value: None,
        classify_validation_error=lambda exc: 'retry', **kwargs)


def test_provider_allowance_resets_only_after_a_correctable_answer():
    calls, traces = [], []

    def invoke(messages):
        calls.append(list(messages))
        if len(calls) % 2:
            raise TimeoutError('provider timeout')
        return {'number': 1 if len(calls) == 6 else 0}

    result = run(SimpleNamespace(invoke=invoke), max_output_attempts=3,
                 max_provider_retries_per_attempt=1,
                 on_attempt=lambda *args: traces.append(args[-1]))
    assert result.parsed.number == 1
    assert (result.output_attempts, result.attempts) == (3, 6)
    assert [(t['output_attempt'], t['provider_call']) for t in traces] == [
        (1, 1), (1, 2), (2, 1), (2, 2), (3, 1), (3, 2)]
    assert [len(messages) for messages in calls] == [2, 2, 3, 3, 4, 4]
    assert calls[0] == calls[1]
    assert calls[2] == calls[3]
    assert calls[4] == calls[5]
    assert traces[1]['correction_scheduled'] is True
    assert traces[2]['correction_message'] is None
    assert traces[0]['next_action'] == 'retry_provider'


def test_exhausted_provider_budget_stops_without_starting_another_output_attempt():
    calls, traces = [], []

    def invoke(messages):
        calls.append(messages)
        raise TimeoutError('provider timeout')

    with pytest.raises(TimeoutError) as caught:
        run(SimpleNamespace(invoke=invoke), max_output_attempts=3,
            max_provider_retries_per_attempt=1,
            on_attempt=lambda *args: traces.append(args[-1]))
    assert len(calls) == 2
    assert caught.value.parser_output_attempts == 1
    assert traces[-1]['budget_exhausted'] == 'provider_retries'
    assert traces[-1]['next_action'] == 'stop'


def test_invalid_answers_exhaust_only_output_budget():
    calls, traces = [], []

    def invoke(messages):
        calls.append(messages)
        return {'number': 0}

    with pytest.raises(ValidationError) as caught:
        run(SimpleNamespace(invoke=invoke), max_output_attempts=3,
            max_provider_retries_per_attempt=2,
            on_attempt=lambda *args: traces.append(args[-1]))
    assert len(calls) == 3
    assert caught.value.parser_output_attempts == 3
    assert [t['provider_call'] for t in traces] == [1, 1, 1]
    assert traces[-1]['budget_exhausted'] == 'output_attempts'


def test_zero_provider_retries_stops_after_first_timeout():
    calls = []

    def invoke(messages):
        calls.append(messages)
        raise TimeoutError('provider timeout')

    with pytest.raises(TimeoutError):
        run(SimpleNamespace(invoke=invoke), max_output_attempts=3,
            max_provider_retries_per_attempt=0)
    assert len(calls) == 1


@pytest.mark.parametrize('name', ['ContentFilterFinishReasonError', 'BlockedPromptException', 'RefusalError'])
def test_refusal_never_falls_back_even_with_retryable_status(name):
    error = type(name, (Exception,), {'status_code': 503})()
    assert provider_failure_category(error) == 'provider_refusal'
    assert not can_fallback(error, FallbackPolicy(http_statuses=[503]))
    calls = []

    def invoke(messages):
        calls.append(messages)
        raise error

    with pytest.raises(type(error)):
        run(SimpleNamespace(invoke=invoke), max_provider_retries_per_attempt=3)
    assert len(calls) == 1


def test_wrapped_transport_failure_has_consistent_trace_and_final_category():
    transport = type('GatewayError', (Exception,), {'code': 429})()
    error = type('OutputParserException', (ValueError,), {})('wrapped error')
    error.__cause__ = transport
    assert not parser_workflow._is_structured_output_error(error)
    assert not semantic_failures.is_validation_failure(error)
    assert parser_workflow._failure_category(error, 'provider') == 'provider_unavailable'
    detail = semantic_failures.failure(error, 'judge_unavailable', 'Provider unavailable.')
    assert detail['failure_category'] == 'provider_unavailable'
    assert detail['validation_errors'] is None


def test_wrapped_schema_error_retains_field_level_json():
    try:
        Answer.model_validate({'number': 0})
    except ValidationError as schema_error:
        error = type('OutputParserException', (ValueError,), {'llm_output': '{"number":0}'})('bad answer')
        error.__cause__ = schema_error
    details = parser_workflow._validation_errors(error, error.llm_output)
    assert details['number'][0]['code'] == 'greater_than_equal'


def test_late_answer_stops_without_retry_or_acceptance(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(parser_workflow, 'monotonic', lambda: now[0])
    calls, traces = [], []

    def invoke(messages):
        calls.append(messages)
        now[0] = 2.0
        return {'number': 1}

    with pytest.raises(TimeoutError):
        run(SimpleNamespace(invoke=invoke), stage_timeout_seconds=1,
            on_attempt=lambda *args: traces.append(args[-1]))
    assert len(calls) == 1
    assert traces[-1]['budget_exhausted'] == 'stage_time'
    assert traces[-1]['response'] == {'number': 1}
