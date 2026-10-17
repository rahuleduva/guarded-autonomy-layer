"""Model-only transaction tests before migration generation; no cloud calls.

The in-memory schema deliberately uses current metadata so these checks can run
without generating or applying migration files. Application startup still uses
Alembic exclusively.
"""
import json
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

from src.main import create_app
from src.config import settings
from src.db.transaction_guard import assert_no_transaction
from src.models.workflow import WorkflowOperation, WorkflowStatus
from src.models.request import ActionRequest, NaturalLanguageRequest
from src.models.clarification import RequestInterpretation, InterpretationStatus
from src.models.decision import DecisionRecord
from src.models.escalation import EscalationQueueItem
from src.services import workflow, workflow_flows, escalation_service, llm_parser, executor
from src.services.policy_loader import seed_policy_artifacts


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setattr(settings, 'OFFLINE_MODE', True)
    monkeypatch.setattr(settings, 'SEMANTIC_PIPELINE_MODE', 'baseline')
    db = create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})
    SQLModel.metadata.create_all(db)
    with Session(db) as session:
        seed_policy_artifacts(session)
        session.commit()
    yield db
    db.dispose()


def body(response):
    return json.loads(response.body) if hasattr(response, 'body') else response.model_dump(mode='json')


def test_duplicate_progress_and_completed_response(engine):
    calls = []
    def work(op):
        duplicate = workflow.run(engine, 'test', 'agent', 'key', {'input': 1}, lambda op: pytest.fail('duplicate ran'))
        assert duplicate.status_code == 202
        calls.append(op.slow('provider', lambda: assert_no_transaction()))
        return op.transaction('done', lambda session, checkpoint: {'result': 'ok', 'capability_token': 'sensitive-token'}, complete=True)
    first = workflow.run(engine, 'test', 'agent', 'key', {'input': 1}, work)
    again = workflow.run(engine, 'test', 'agent', 'key', {'input': 1}, lambda op: pytest.fail('completed ran'))
    assert body(again) == body(first)
    assert len(calls) == 1
    with Session(engine) as session:
        row = session.exec(select(WorkflowOperation)).one()
        assert 'sensitive-token' not in json.dumps(row.response)
    with pytest.raises(HTTPException) as caught:
        workflow.run(engine, 'test', 'agent', 'key', {'input': 2}, work)
    assert caught.value.status_code == 409


def test_expired_lease_fences_old_worker_and_preserves_checkpoint(engine):
    old = []
    def interrupted(op):
        old.append(op)
        op.transaction('saved', lambda session, cp: cp.update(saved=True))
        return {'interrupted': True}
    workflow.run(engine, 'test', 'agent', 'key', {}, interrupted)
    with Session(engine) as session:
        row = session.exec(select(WorkflowOperation)).one()
        row.lease_until = workflow.now() - timedelta(seconds=1)
        session.add(row)
        session.commit()
    def resumed(op):
        op.transaction('resumed', lambda session, cp: None)
        assert op.checkpoint['saved']
        with pytest.raises(HTTPException):
            old[0].transaction('obsolete', lambda session, cp: cp.update(overwritten=True))
        return op.transaction('done', lambda session, cp: {'ok': True}, complete=True)
    assert body(workflow.run(engine, 'test', 'agent', 'key', {}, resumed)) == {'ok': True}


def test_provider_failure_resumes_without_losing_checkpoint(engine):
    def fails(op):
        op.transaction('saved', lambda session, cp: cp.update(stage_one='done'))
        op.slow('provider', lambda: (_ for _ in ()).throw(RuntimeError('unavailable')))
    with pytest.raises(RuntimeError):
        workflow.run(engine, 'test', 'agent', 'key', {}, fails)
    def resumes(op):
        op.transaction('resuming', lambda session, cp: None)
        assert op.checkpoint['stage_one'] == 'done'
        return op.transaction('done', lambda session, cp: {'ok': True}, complete=True)
    assert body(workflow.run(engine, 'test', 'agent', 'key', {}, resumes)) == {'ok': True}


def test_guard_rejects_open_read_transaction(engine):
    with Session(engine) as session:
        session.exec(select(WorkflowOperation)).all()
        with pytest.raises(RuntimeError, match='open database transaction'):
            assert_no_transaction()
        session.rollback()
        assert_no_transaction()


def test_evaluation_and_explanation_have_no_open_transaction(engine, monkeypatch):
    from src.services.advisory_evidence import build_evidence
    def evidence(action):
        assert_no_transaction()
        return build_evidence(action)
    monkeypatch.setattr(workflow_flows, 'build_evidence', evidence)
    original = escalation_service.prepare_package
    def explanation(context):
        assert_no_transaction()
        with Session(engine) as session:
            row = session.exec(select(EscalationQueueItem)).one()
            assert row.status == 'PREPARING'
            assert escalation_service.list_pending(session) == []
        return original(context)
    monkeypatch.setattr(escalation_service, 'prepare_package', explanation)
    action = ActionRequest(agent_id='test-agent', actor_role='workspace_agent',
        action_class='read_file', target_resource='/etc/passwd')
    result = workflow.run(engine, 'evaluate', 'test-agent', action.request_id, action,
                          lambda op: workflow_flows.evaluate(op, action))
    assert body(result)['decision'] == 'DENIED'
    # A genuinely held request exercises explanation staging.
    action = action.model_copy(update={'request_id': 'held-request', 'action_class': 'delete_file',
        'target_resource': '/srv/agent/workspace/test.txt', 'actor_role': 'admin_agent'})
    result = workflow.run(engine, 'evaluate', 'test-agent', action.request_id, action,
                          lambda op: workflow_flows.evaluate(op, action))
    assert body(result)['decision'] in {'ESCALATED', 'PENDING_APPROVAL'}
    with Session(engine) as session:
        row = session.exec(select(EscalationQueueItem)).one()
        assert row.status == 'PENDING'
        assert row.package_json['package_fingerprint']


def test_parser_reservation_is_committed_before_call(engine):
    source = NaturalLanguageRequest(agent_id='agent', actor_role='workspace_agent', raw_prompt='Read /etc/passwd')
    def parse(request):
        assert_no_transaction()
        with Session(engine) as session:
            row = session.exec(select(RequestInterpretation)).one()
            assert row.status == InterpretationStatus.PROCESSING.value
        return ActionRequest(request_id=request.request_id, agent_id=request.agent_id,
            actor_role=request.actor_role, action_class='read_file', target_resource='/etc/passwd')
    result = workflow.run(engine, 'interpret', 'agent', source.request_id, source,
                          lambda op: workflow_flows.interpret(op, source, parse))
    assert body(result)['decision'] == 'DENIED'
    with Session(engine) as session:
        row = session.exec(select(RequestInterpretation)).one()
        assert row.status == InterpretationStatus.EVALUATED.value
        assert len(row.turns) == 1
        assert session.get(DecisionRecord, row.decision_id)


def test_parser_rejection_is_recorded_and_cached(engine):
    source = NaturalLanguageRequest(agent_id='agent', actor_role='workspace_agent', raw_prompt='Do something')
    def reject(request):
        raise llm_parser.ParserRejectionError('unsupported action')
    result = workflow.run(engine, 'interpret', 'agent', source.request_id, source,
                          lambda op: workflow_flows.interpret(op, source, reject))
    assert result.status_code == 400
    duplicate = workflow.run(engine, 'interpret', 'agent', source.request_id, source,
                             lambda op: pytest.fail('parser ran again'))
    assert body(duplicate) == body(result)
    with Session(engine) as session:
        assert session.exec(select(DecisionRecord)).one().decision_stage == 'PARSER'


def test_finalize_failure_rolls_back_business_writes(engine):
    def work(op):
        def finalize(session, checkpoint):
            session.add(RequestInterpretation(agent_id='agent', actor_role='role', original_prompt='x', status='processing'))
            session.flush()
            raise RuntimeError('finalization failed')
        return op.transaction('done', finalize, complete=True)
    with pytest.raises(RuntimeError):
        workflow.run(engine, 'test', 'agent', 'key', {}, work)
    with Session(engine) as session:
        assert session.exec(select(RequestInterpretation)).all() == []
        assert session.exec(select(WorkflowOperation)).one().status == WorkflowStatus.FAILED.value


def test_api_idempotency_and_status_endpoint(engine):
    from fastapi.testclient import TestClient
    with TestClient(create_app(engine)) as client:
        payload = dict(request_id='api-request', agent_id='agent', actor_role='workspace_agent',
                       action_class='read_file', target_resource='/etc/passwd')
        first = client.post('/api/v1/actions/evaluate', json=payload)
        assert first.status_code == 200
        again = client.post('/api/v1/actions/evaluate', json=payload)
        assert again.json() == first.json()
        operation = client.get('/api/v1/operations/' + first.headers['X-Operation-ID'])
        assert operation.json()['status'] == 'COMPLETED'
        changed = client.post('/api/v1/actions/evaluate', json={**payload, 'target_resource': '/etc/hosts'})
        assert changed.status_code == 409
        with Session(engine) as session:
            assert len(session.exec(select(DecisionRecord)).all()) == 1


def test_review_preparation_resumes_without_repeating_decision(engine, monkeypatch):
    original = escalation_service.prepare_package
    calls = []
    def unavailable(context):
        calls.append(True)
        with Session(engine) as session:
            row = session.exec(select(EscalationQueueItem)).one()
            from src.models.request import ApprovalDecision
            with pytest.raises(escalation_service.ReviewConflictError):
                escalation_service.decide(session, row.decision_id, ApprovalDecision(
                    decision_id=row.decision_id, reviewer_id='reviewer', approve=False,
                    note='Reject', package_fingerprint='0' * 64))
        raise RuntimeError('simulated interrupted explanation')
    monkeypatch.setattr(escalation_service, 'prepare_package', unavailable)
    action = ActionRequest(request_id='held', agent_id='agent', actor_role='admin_agent',
        action_class='delete_file', target_resource='/srv/agent/workspace/test.txt')
    run = lambda: workflow.run(engine, 'evaluate', 'agent', action.request_id, action,
                               lambda op: workflow_flows.evaluate(op, action))
    with pytest.raises(RuntimeError):
        run()
    monkeypatch.setattr(escalation_service, 'prepare_package', original)
    result = run()
    assert body(result)['decision'] == 'ESCALATED'
    with Session(engine) as session:
        assert len(session.exec(select(DecisionRecord)).all()) == 1
        assert session.exec(select(EscalationQueueItem)).one().status == 'PENDING'


def test_terminal_analysis_failure_is_recorded_on_interpretation(engine, monkeypatch):
    source = NaturalLanguageRequest(agent_id='agent', actor_role='workspace_agent', raw_prompt='Read /etc/passwd')
    def parse(request):
        return ActionRequest(request_id=request.request_id, agent_id=request.agent_id,
            actor_role=request.actor_role, action_class='read_file', target_resource='/etc/passwd')
    monkeypatch.setattr(workflow_flows, 'build_evidence', lambda action: (_ for _ in ()).throw(ValueError('action conflict')))
    with pytest.raises(ValueError):
        workflow.run(engine, 'interpret', 'agent', source.request_id, source,
                     lambda op: workflow_flows.interpret(op, source, parse))
    with Session(engine) as session:
        row = session.exec(select(RequestInterpretation)).one()
        assert row.status == InterpretationStatus.PROCESSING_FAILED.value
        assert row.turns[-1]['error_message'] == 'action conflict'
        assert session.exec(select(DecisionRecord)).all() == []


@pytest.mark.parametrize('change_resource', [False, True])
def test_rollback_rechecks_resource_after_slow_work(engine, monkeypatch, change_resource):
    from src.models.autonomy import AutonomyState
    from src.models.runtime import SimulatedResource
    with Session(engine) as session:
        for action_class in ('create_file', 'update_file'):
            session.add(AutonomyState(agent_id='agent', action_class=action_class, mode='LIVE', streak=0))
        session.commit()
    action = ActionRequest(agent_id='agent', actor_role='workspace_agent', action_class='create_file',
        target_resource='/srv/agent/workspace/file.txt', environment='development')
    allowed = body(workflow.run(engine, 'evaluate', 'agent', action.request_id, action,
                                lambda op: workflow_flows.evaluate(op, action)))
    assert allowed['decision'] == 'ALLOWED'
    with Session(engine) as session:
        executed = executor.execute(session, allowed['decision_id'], allowed['capability_token'])
        execution_id = executed.execution_id
        session.commit()
    from src.services.advisory_evidence import build_evidence
    def slow(action):
        assert_no_transaction()
        if change_resource:
            with Session(engine) as session:
                row = session.get(SimulatedResource, '/srv/agent/workspace/file.txt')
                row.value_json = {'kind': 'file', 'content': 'newer content'}
                session.add(row)
                session.commit()
        return build_evidence(action)
    monkeypatch.setattr(workflow_flows, 'build_evidence', slow)
    result = workflow.run(engine, 'rollback', execution_id, execution_id, {},
                          lambda op: workflow_flows.rollback(op, execution_id))
    assert body(result)['status'] == ('FAILED' if change_resource else 'COMPENSATED')


def test_exhausted_parser_provider_failure_can_retry_without_erasing_history(engine):
    source = NaturalLanguageRequest(agent_id='agent', actor_role='workspace_agent', raw_prompt='Read /etc/passwd')
    calls = []
    def parse(request):
        assert_no_transaction()
        calls.append(True)
        if len(calls) == 1:
            error = llm_parser.OnlineActionParseError('provider temporarily unavailable')
            error.parser_feedback = 'Retry after the provider recovers.'
            error.validation_errors = {'provider': [{'code': 'unavailable', 'message': str(error)}]}
            raise error
        return ActionRequest(request_id=request.request_id, agent_id=request.agent_id,
            actor_role=request.actor_role, action_class='read_file', target_resource='/etc/passwd')
    run = lambda: workflow.run(engine, 'interpret', 'agent', source.request_id, source,
                               lambda op: workflow_flows.interpret(op, source, parse))
    assert run().status_code == 502
    with Session(engine) as session:
        row = session.exec(select(WorkflowOperation)).one()
        assert row.status == WorkflowStatus.FAILED.value and row.retryable
    assert body(run())['decision'] == 'DENIED'
    with Session(engine) as session:
        row = session.exec(select(RequestInterpretation)).one()
        assert row.revision == 2
        assert [turn['status'] for turn in row.turns] == ['provider_failed', 'evaluated']
        assert row.turns[0]['validation_errors']['provider'][0]['code'] == 'unavailable'
        assert row.turns[0]['correction_message'] == 'Retry after the provider recovers.'
        assert len(session.exec(select(DecisionRecord)).all()) == 1
    run()
    assert len(calls) == 2


def test_clarification_api_reserves_revision_and_reuses_completed_answer(engine, monkeypatch):
    from fastapi.testclient import TestClient
    calls = []
    def parse(request):
        assert_no_transaction()
        calls.append(request.raw_prompt)
        if len(calls) == 1:
            raise llm_parser.ClarificationRequired('target_resource', 'missing', 'Which file should be read?')
        return ActionRequest(request_id=request.request_id, agent_id=request.agent_id,
            actor_role=request.actor_role, action_class='read_file', target_resource='/etc/passwd')
    monkeypatch.setattr(llm_parser, 'parse', parse)
    with TestClient(create_app(engine)) as client:
        first = client.post('/api/v1/actions/evaluate-prompt', json={
            'request_id': 'clarification-test', 'agent_id': 'agent', 'actor_role': 'workspace_agent',
            'raw_prompt': 'Read the file'})
        assert first.status_code == 202
        pending = first.json()
        url = '/api/v1/actions/clarifications/' + pending['interpretation_id']
        answer = dict(agent_id='agent', actor_role='workspace_agent', revision=1,
                      clarified_prompt='Read /etc/passwd')
        completed = client.post(url, json=answer)
        assert completed.status_code == 200
        assert completed.json()['decision'] == 'DENIED'
        assert client.post(url, json=answer).json() == completed.json()
        assert len(calls) == 2
        with Session(engine) as session:
            row = session.get(RequestInterpretation, pending['interpretation_id'])
            assert [turn['status'] for turn in row.turns] == ['needs_clarification', 'evaluated']


def test_review_and_execution_are_idempotent_and_cloud_free(engine, monkeypatch):
    from fastapi.testclient import TestClient
    from src.models.autonomy import AutonomyState
    from src.models.human_review import HumanReviewRecord
    from src.models.policy import Policy
    from src.models.runtime import ExecutionRecord
    with Session(engine) as session:
        session.add(AutonomyState(agent_id='agent', action_class='create_file', mode='ASSISTED', streak=0))
        versions = [row.version for row in session.exec(select(Policy)).all()]
        session.commit()
    with TestClient(create_app(engine)) as client:
        evaluated = client.post('/api/v1/actions/evaluate', json=dict(
            request_id='review-test', agent_id='agent', actor_role='workspace_agent',
            action_class='create_file', target_resource='/srv/agent/workspace/new.txt', environment='development')).json()
        assert evaluated['decision'] == 'PENDING_APPROVAL'
        package = client.get('/api/v1/escalations').json()[0]
        monkeypatch.setattr(workflow_flows, 'build_evidence', lambda action: pytest.fail('review called semantic provider'))
        payload = dict(decision_id=evaluated['decision_id'], reviewer_id='reviewer', approve=True,
                       note='Reviewed this file creation.', package_fingerprint=package['package_fingerprint'])
        url = '/api/v1/control/approve/' + evaluated['decision_id']
        approved = client.post(url, json=payload)
        assert approved.status_code == 200
        authorization = approved.json()
        assert authorization['authorization_status'] == 'ALLOWED'
        assert client.post(url, json=payload).json() == authorization
        execution_payload = dict(decision_id=authorization['authorization_decision_id'], token=authorization['capability_token'])
        executed = client.post('/api/v1/actions/execute', json=execution_payload)
        assert executed.json()['status'] == 'EXECUTED'
        assert client.post('/api/v1/actions/execute', json=execution_payload).json() == executed.json()
        consumed = client.post('/api/v1/actions/execute', json=execution_payload,
                               headers={'Idempotency-Key': 'independent-token-reuse'})
        assert consumed.json()['status'] == 'REJECTED'
        with Session(engine) as session:
            assert len(session.exec(select(HumanReviewRecord)).all()) == 1
            assert len(session.exec(select(ExecutionRecord)).all()) == 1
            assert [row.version for row in session.exec(select(Policy)).all()] == versions
            for row in session.exec(select(WorkflowOperation)).all():
                assert authorization['capability_token'] not in json.dumps(row.model_dump(mode='json'))


def test_finalization_uses_current_policy_and_autonomy_after_cloud_work(engine, monkeypatch):
    from src.models.autonomy import AutonomyState
    from src.models.policy import Policy
    from src.services.policy_store import activate
    from src.services.advisory_evidence import build_evidence
    selected = []
    def slow(action):
        assert_no_transaction()
        evidence = build_evidence(action)
        with Session(engine) as session:
            candidate = session.exec(select(Policy).where(Policy.is_shadow == True)).one()
            selected.append(candidate.policy_hash)
            activate(session, candidate, 'reviewer')
            session.add(AutonomyState(agent_id='agent', action_class='create_file', mode='LIVE', streak=0))
            session.commit()
        return evidence
    monkeypatch.setattr(workflow_flows, 'build_evidence', slow)
    action = ActionRequest(agent_id='agent', actor_role='workspace_agent', action_class='create_file',
        target_resource='/srv/agent/workspace/new.txt', environment='development')
    result = body(workflow.run(engine, 'evaluate', 'agent', action.request_id, action,
                               lambda op: workflow_flows.evaluate(op, action)))
    assert result['policy_hash'] == selected[0]
    assert result['autonomy_level'] == 'LIVE'
    with Session(engine) as session:
        decision = session.get(DecisionRecord, result['decision_id'])
        assert decision.evidence_json['autonomy_snapshot']['mode'] == 'LIVE'


def test_failure_cause_chain_is_retained_but_status_uses_public_summary(engine):
    def work(op):
        try:
            raise ValueError('specific validation details')
        except ValueError as cause:
            raise RuntimeError('workflow could not complete') from cause
    with pytest.raises(RuntimeError):
        workflow.run(engine, 'test', 'agent', 'key', {}, work)
    with Session(engine) as session:
        row = session.exec(select(WorkflowOperation)).one()
        assert row.error['error_message'] == 'workflow could not complete'
        assert row.error['exception_chain'][1]['message'] == 'specific validation details'
        public = workflow.status(session, row.operation_id)
        assert 'exception_chain' not in public['error']
