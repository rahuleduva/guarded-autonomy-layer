"""Policy shadow persistence, authority, and transaction boundaries (EVAL-6)."""

import copy

import pytest
from sqlalchemy import event
from sqlmodel import SQLModel, Session, create_engine, select

from src.models.autonomy import AutonomyState
from src.models.decision import DecisionRecord
from src.models.enums import DecisionOutcome
from src.models.policy import Policy
from src.models.policy_shadow import ShadowRecord
from src.models.request import ActionRequest
from src.models.token import ConsumedToken
from src.config import settings
from src.services.policy_loader import canonical_policy_hash, seed_policy_artifacts
from src.services.shadow_engine import evaluate_and_record


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setattr(settings, "OFFLINE_MODE", True)
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    SQLModel.metadata.create_all(engine, tables=[
        Policy.__table__, DecisionRecord.__table__, ShadowRecord.__table__,
        AutonomyState.__table__, ConsumedToken.__table__,
    ])
    with Session(engine) as db:
        seed_policy_artifacts(db)
        db.commit()
        yield db
    engine.dispose()


def request(**overrides):
    values = dict(
        agent_id="agent-1", actor_role="workspace_agent",
        action_class="read_file", target_resource="/srv/agent/workspace/.env",
    )
    values.update(overrides)
    return ActionRequest(**values)


def autonomy(mode):
    return AutonomyState(agent_id="agent-1", action_class="read_file", mode=mode, streak=7)


def test_candidate_denial_is_persisted_and_active_remains_allowed(session):
    evidence = {"blast_radius": {"files_touched": 1}, "semantic_warning": None}
    original = copy.deepcopy(evidence)
    state = autonomy("LIVE")
    result, decision, shadow = evaluate_and_record(session, request(), evidence, state)
    session.commit()

    assert result.outcome == DecisionOutcome.ALLOWED
    assert decision.outcome == "ALLOWED"
    assert shadow.would_have_decided == "DENIED"
    assert shadow.decision_id == decision.decision_id
    candidate = session.exec(select(Policy).where(Policy.is_shadow == True)).one()
    assert shadow.candidate_policy_hash == candidate.policy_hash
    assert session.get(ShadowRecord, shadow.shadow_id).would_have_decided == "DENIED"
    assert decision.policy_hash != shadow.candidate_policy_hash
    assert evidence == original
    assert state.mode == "LIVE" and state.streak == 7
    assert session.exec(select(ConsumedToken)).all() == []
    assert session.exec(select(AutonomyState)).all() == []
    assert session.exec(select(Policy).where(Policy.is_active == True)).one().version == "1.0.0"


@pytest.mark.parametrize("mode,outcome", [
    ("SHADOW", "SHADOW_LOGGED"), ("ASSISTED", "PENDING_APPROVAL"), ("LIVE", "ALLOWED"),
])
def test_both_policies_use_same_autonomy_snapshot(session, mode, outcome):
    result, _, shadow = evaluate_and_record(
        session, request(target_resource="/srv/agent/workspace/notes.txt"),
        autonomy_state=autonomy(mode),
    )
    assert result.outcome.value == shadow.would_have_decided == outcome


def test_permissive_candidate_cannot_override_active_denial(session):
    candidate = session.exec(select(Policy).where(Policy.is_shadow == True)).one()
    # Replace the candidate fixture before any audit references it.
    rules = copy.deepcopy(candidate.rules_json)
    rules["role_permissions"]["read_only_agent"].append("create_file")
    candidate.rules_json = rules
    candidate.policy_hash = canonical_policy_hash(rules)
    session.commit()
    result, decision, shadow = evaluate_and_record(
        session, request(actor_role="read_only_agent", action_class="create_file",
                         target_resource="/srv/agent/workspace/notes.txt"),
        autonomy_state=autonomy("LIVE"),
    )
    assert result.outcome == DecisionOutcome.DENIED
    assert decision.outcome == "DENIED"
    assert shadow.would_have_decided == "ALLOWED"


def test_active_evaluation_works_without_candidate(session):
    candidate = session.exec(select(Policy).where(Policy.is_shadow == True)).one()
    candidate.is_shadow = False
    session.commit()
    result, decision, shadow = evaluate_and_record(
        session, request(target_resource="/srv/agent/workspace/notes.txt")
    )
    assert result.outcome == DecisionOutcome.SHADOW_LOGGED
    assert decision.outcome == "SHADOW_LOGGED"
    assert shadow is None
    assert session.exec(select(ShadowRecord)).all() == []


def test_missing_active_policy_aborts_before_logging(session):
    active = session.exec(select(Policy).where(Policy.is_active == True)).one()
    active.is_active = False
    active.activated_at = None
    session.commit()
    with pytest.raises(ValueError, match="without an active policy"):
        evaluate_and_record(session, request())
    assert session.exec(select(DecisionRecord)).all() == []
    assert session.exec(select(ShadowRecord)).all() == []


def test_caller_rollback_removes_both_audit_records(session):
    evaluate_and_record(session, request())
    session.rollback()
    assert session.exec(select(DecisionRecord)).all() == []
    assert session.exec(select(ShadowRecord)).all() == []


def test_failed_shadow_write_propagates_and_active_record_rolls_back(session):
    def fail_insert(*_):
        raise RuntimeError("shadow audit unavailable")

    event.listen(ShadowRecord, "before_insert", fail_insert)
    try:
        with pytest.raises(RuntimeError, match="shadow audit unavailable"):
            evaluate_and_record(session, request())
        session.rollback()
    finally:
        event.remove(ShadowRecord, "before_insert", fail_insert)
    assert session.exec(select(DecisionRecord)).all() == []
    assert session.exec(select(ShadowRecord)).all() == []
