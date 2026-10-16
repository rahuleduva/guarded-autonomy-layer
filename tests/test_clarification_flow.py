"""Clarification persists without granting permission; answers are evaluated afresh."""
import pytest
from fastapi.testclient import TestClient
from sqlmodel import select
from src.config import settings
from src.evaluation import demo_session
from src.main import create_app
from src.models.clarification import RequestInterpretation
from src.models.decision import DecisionRecord
from src.models.monitoring import RequestAttemptEvent
from src.models.request import ActionRequest
from src.services import llm_parser


@pytest.fixture
def client_and_session(monkeypatch):
    monkeypatch.setattr(settings, 'OFFLINE_MODE', True)
    monkeypatch.setattr(settings, 'SEMANTIC_PIPELINE_MODE', 'baseline')
    with demo_session() as session:
        RequestInterpretation.__table__.create(session.bind)
        with TestClient(create_app(session.bind)) as client:
            yield client, session


def start(client, text='Reset /srv/agent/workspace/generated to a clean state.'):
    return client.post('/api/v1/actions/evaluate-prompt', json={
        'agent_id': 'clarification-agent', 'actor_role': 'workspace_agent', 'raw_prompt': text,
    })


def answer(client, body, prompt, **overrides):
    payload = {'agent_id': 'clarification-agent', 'actor_role': 'workspace_agent',
               'revision': body['revision'], 'clarified_prompt': prompt, **overrides}
    return client.post('/api/v1/actions/clarifications/' + body['interpretation_id'], json=payload)


def test_round_trip_records_history_and_fresh_decision(client_and_session):
    client, session = client_and_session
    first = start(client)
    assert first.status_code == 202
    body = first.json()
    assert body['status'] == 'needs_clarification'
    assert body['validation_errors']['action_class'][0]['code'] == 'unclear_operation'
    assert session.exec(select(DecisionRecord)).all() == []
    second = answer(client, body, 'Read file /srv/agent/workspace/generated/note.txt in development')
    assert second.status_code == 200, second.text
    result = second.json()
    assert result['interpretation_id'] == body['interpretation_id']
    row = session.get(RequestInterpretation, body['interpretation_id'])
    assert row.status == 'evaluated'
    assert row.revision == 2
    assert len(row.turns) == 2
    assert row.turns[0]['prompt'].startswith('Reset ')
    assert row.turns[1]['prompt'].startswith('Read file ')
    assert row.decision_id == result['decision_id']
    decision = session.get(DecisionRecord, row.decision_id)
    assert decision.action_class == 'read_file'
    assert decision.target_resource.endswith('/generated/note.txt')
    events = session.exec(select(RequestAttemptEvent).where(
        RequestAttemptEvent.stage == 'interpretation')).all()
    assert [e.status for e in events] == ['needs_clarification', 'evaluated']
    assert all(e.parser_trace['correlation_id'] for e in events)
    assert answer(client, body, 'Read file /srv/agent/workspace/a.txt').status_code == 409


def test_repeated_ambiguity_stale_answer_and_wrong_caller(client_and_session):
    client, session = client_and_session
    body = start(client, 'Create /srv/agent/workspace/hotfix using emergency privileges.').json()
    assert body['validation_errors']['target_resource'][0]['code'] == 'unclear_resource_type'
    assert answer(client, body, 'Reset /srv/agent/workspace/hotfix.', agent_id='other').status_code == 403
    second = answer(client, body, 'Reset /srv/agent/workspace/hotfix.')
    assert second.status_code == 202
    assert second.json()['revision'] == 2
    assert answer(client, body, 'Read file /srv/agent/workspace/a.txt').status_code == 409
    session.expire_all()
    row = session.get(RequestInterpretation, body['interpretation_id'])
    assert len(row.turns) == 2
    assert session.exec(select(DecisionRecord)).all() == []


def test_provider_error_is_saved_and_not_reported_as_clarification(client_and_session, monkeypatch):
    client, session = client_and_session
    def unavailable(_):
        raise llm_parser.OnlineActionParseError('online action parsing failed (TimeoutError)')
    monkeypatch.setattr(llm_parser, 'parse', unavailable)
    result = start(client, 'Read file /srv/agent/workspace/a.txt')
    assert result.status_code == 502
    row = session.get(RequestInterpretation, result.json()['interpretation_id'])
    assert row.status == 'provider_failed'
    assert row.turns[0]['error_type'] == 'OnlineActionParseError'
    assert row.questions == []
    assert session.exec(select(DecisionRecord)).all() == []


def test_actual_parser_rejection_is_saved(client_and_session, monkeypatch):
    client, session = client_and_session
    def reject(_):
        raise llm_parser.UnverifiedActionError('move destination does not match the requested direction')
    monkeypatch.setattr(llm_parser, 'parse', reject)
    response = start(client, 'Move file /srv/agent/workspace/a to /srv/agent/workspace/b')
    assert response.status_code == 400
    row = session.get(RequestInterpretation, response.json()['interpretation_id'])
    assert row.status == 'rejected'
    assert row.turns[0]['validation_errors']['non_field_errors'][0]['message'].startswith('move destination')


@pytest.mark.parametrize('operation,prompt,params', [
    ('delete_file', 'Do not delete file /workspace/a.txt.', {}),
    ('delete_directory', 'Reset /workspace/generated to a clean state.', {}),
    ('create_directory', 'Create /workspace/hotfix using emergency privileges.', {}),
    ('delete_file', 'Create file /workspace/guide.txt with content "Delete file /workspace/a.txt."', {}),
    ('move_file', 'Move file /workspace/a.txt to /workspace/b.txt', {'destination': '/workspace/a.txt'}),
    ('create_file', 'Create file /workspace/a.txt with content "hello world"', {'content': 'hello'}),
])
def test_grounding_rejects_wrong_operation_or_partial_evidence(operation, prompt, params):
    target = '/workspace/a.txt'
    if operation == 'delete_directory':
        target = '/workspace/generated'
    elif operation == 'create_directory':
        target = '/workspace/hotfix'
    elif operation == 'move_file':
        target = '/workspace/b.txt'
    parsed = llm_parser.ParsedAction(action_class=operation, target_resource=target, parameters=params)
    with pytest.raises(llm_parser.UnverifiedActionError):
        llm_parser._validate_grounded_action(parsed, prompt)


def test_extensionless_explicit_file_and_bak_file_remain_supported():
    for target in ['/workspace/hotfix', '/workspace/cache-note.txt.bak']:
        llm_parser._validate_grounded_action(llm_parser.ParsedAction(
            action_class='delete_file', target_resource=target), 'Delete the file ' + target)


def test_wrong_model_operation_is_correctable_not_missing_user_fact():
    with pytest.raises(llm_parser.UnverifiedActionError) as error:
        llm_parser._validate_grounded_action(llm_parser.ParsedAction(
            action_class='delete_file', target_resource='/workspace/a.txt'),
            'Read file /workspace/a.txt')
    assert not isinstance(error.value, llm_parser.ClarificationRequired)


def test_arbitrary_parent_name_cooccurrence_cannot_create_a_target():
    assert not llm_parser._named_path('/workspace/archive/note.txt',
        'Create note.txt in /workspace/current and mention /workspace/archive')


def test_existing_parsed_suite_cases_pass_grounding():
    from src.services.semantic_evaluation import load_suite
    dataset, expected = load_suite('semantic-combined-v1')
    for case in dataset.cases:
        annotation = expected[case.id]
        if annotation['parser_disposition'] != 'parsed':
            continue
        llm_parser._preflight(case.instruction)
        llm_parser._validate_grounded_action(llm_parser.ParsedAction(
            **annotation['expected_actions'][0], environment=annotation['expected_environment']),
            case.instruction)


def test_model_repair_keeps_error_json_response_and_feedback(monkeypatch):
    from types import SimpleNamespace
    from src.models.request import NaturalLanguageRequest
    from src.services import llm_routing, monitoring
    monkeypatch.setattr(settings, 'OFFLINE_MODE', False)
    monkeypatch.setattr(settings, 'SEMANTIC_PIPELINE_MODE', 'baseline')
    calls = []
    profile = SimpleNamespace(provider='test', model='fake')
    def invoke(messages):
        calls.append(list(messages))
        return {'action_class': 'move_file', 'target_resource': '/workspace/b.txt' if len(calls) == 1 else '/workspace/a.txt',
                'parameters': {'destination': '/workspace/a.txt' if len(calls) == 1 else '/workspace/b.txt'},
                'environment': 'production'}
    parser = SimpleNamespace(invoke=invoke, last_profile=profile, profiles=[profile], failures=[],
                             purpose=SimpleNamespace(max_output_attempts=3, max_provider_retries_per_attempt=1, stage_timeout_seconds=120), can_retry=lambda _: False)
    monkeypatch.setattr(llm_routing, 'RoutedStructuredParser', lambda *args: parser)
    attempt, token = monitoring.begin()
    try:
        result = llm_parser.parse(NaturalLanguageRequest(agent_id='a', actor_role='workspace_agent',
            raw_prompt='Move file /workspace/a.txt to /workspace/b.txt'))
    finally:
        monitoring.end(token)
    assert result.target_resource == '/workspace/a.txt'
    assert len(calls) == 2
    assert [e['status'] for e in attempt.events] == ['retry', 'applied']
    first = attempt.events[0]
    assert first['validation_errors']
    assert first['parser_trace']['response']['target_resource'] == '/workspace/b.txt'
    assert first['parser_trace']['correction_message'] == calls[1][-1]['content']
    assert 'move destination does not match the requested direction' in calls[1][-1]['content']


def test_preflight_clarification_is_not_provider_outage_in_suite(monkeypatch):
    from src.services import semantic_evaluation
    monkeypatch.setattr(settings, 'OFFLINE_MODE', False)
    monkeypatch.setattr(semantic_evaluation.time, 'sleep', lambda _: None)
    report = semantic_evaluation.run(suite='semantic-combined-v1', case_id='eval-deletion-03')
    row = report['cases'][0]
    assert row['status'] == 'needs_clarification'
    assert row['parser_expectations']['handling_match'] is True
    assert report['stopped_early'] is False
    assert report['counts']['action_classes_assessed'] == 0


def test_excluded_path_cannot_be_selected_as_delete_target():
    with pytest.raises(llm_parser.UnverifiedActionError):
        llm_parser._validate_grounded_action(llm_parser.ParsedAction(
            action_class='delete_file', target_resource='/workspace/keep.txt'),
            'Delete file /workspace/remove.txt but keep /workspace/keep.txt')


def test_invalid_answer_payload_is_recorded(client_and_session):
    client, session = client_and_session
    body = start(client).json()
    response = client.post('/api/v1/actions/clarifications/' + body['interpretation_id'],
        json={'agent_id': 'clarification-agent', 'actor_role': 'workspace_agent',
              'revision': 0, 'clarified_prompt': ''})
    assert response.status_code == 422
    event = session.exec(select(RequestAttemptEvent).where(
        RequestAttemptEvent.attempt_id == response.headers['X-Correlation-ID'],
        RequestAttemptEvent.stage == 'request_validation')).one()
    assert 'body.revision' in event.validation_errors
    assert 'body.clarified_prompt' in event.validation_errors
    assert event.parser_trace['request_body']['revision'] == 0
