"""Persisted autonomy, earned promotions, and reviewer audit (REQ-9/EVAL-9)."""

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, inspect
from sqlmodel import SQLModel, Session, create_engine, select

from src.config import settings
from src.models.autonomy import AutonomyPromotionEvent, AutonomyState
from src.models.decision import DecisionRecord
from src.models.enums import DecisionOutcome
from src.models.policy import Policy
from src.models.policy_shadow import ShadowRecord
from src.models.request import ActionRequest
from src.services import autonomy_service
from src.services.policy_loader import seed_policy_artifacts
from src.services.shadow_engine import evaluate_and_record


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OFFLINE_MODE", True)
    monkeypatch.setattr(settings, "PROMOTION_THRESHOLD_ASSISTED", 2)
    monkeypatch.setattr(settings, "PROMOTION_THRESHOLD_LIVE", 4)
    database = create_engine(f"sqlite:///{tmp_path / 'autonomy.db'}")

    @event.listens_for(database, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    SQLModel.metadata.create_all(database, tables=[
        Policy.__table__, DecisionRecord.__table__, ShadowRecord.__table__,
        AutonomyState.__table__, AutonomyPromotionEvent.__table__,
    ])
    with Session(database) as session:
        seed_policy_artifacts(session)
        session.commit()
    yield database
    database.dispose()


def run(session, agent_id="agent-1", action_class="read_file", target=None):
    action = ActionRequest(
        agent_id=agent_id, action_class=action_class, actor_role="workspace_agent",
        target_resource=target or "/srv/agent/workspace/notes.txt",
        environment="development",
    )
    return evaluate_and_record(session, action)


def test_first_action_is_shadow_and_agent_action_pairs_are_separate(engine):
    with Session(engine) as session:
        result, decision, _ = run(session)
        assert result.outcome == DecisionOutcome.SHADOW_LOGGED
        assert result.ttl_seconds is None
        state = autonomy_service.get(session, "agent-1", "read_file")
        assert state.mode == "SHADOW" and state.streak == 1
        assert decision.evidence_json["autonomy_snapshot"]["mode"] == "SHADOW"
        assert decision.evidence_json["autonomy_snapshot"]["streak"] == 0
        assert autonomy_service.get(session, "agent-2", "read_file").streak == 0
        assert autonomy_service.get(session, "agent-1", "create_file").mode == "SHADOW"


def test_streak_and_promotion_survive_new_database_session(engine):
    with Session(engine) as session:
        run(session)
        session.commit()
    with Session(engine) as session:
        state = autonomy_service.get(session, "agent-1", "read_file")
        assert state.mode == "SHADOW" and state.streak == 1
        result, _, _ = run(session)
        assert result.outcome == DecisionOutcome.SHADOW_LOGGED
        assert state.mode == "ASSISTED" and state.streak == 2
        session.commit()
    with Session(engine) as session:
        state = autonomy_service.get(session, "agent-1", "read_file")
        assert state.mode == "ASSISTED" and state.promoted_at is not None
        promotion = session.exec(select(AutonomyPromotionEvent)).one()
        assert promotion.promoted and promotion.reviewer_id is None
        assert len(promotion.evidence_json["decision_ids"]) == 2
        assert promotion.previous_mode == "SHADOW"
        assert promotion.requested_mode == "ASSISTED"


def test_clean_assisted_requests_qualify_but_never_automatically_go_live(engine):
    with Session(engine) as session:
        for _ in range(4):
            result, _, _ = run(session)
        state = autonomy_service.get(session, "agent-1", "read_file")
        assert state.mode == "ASSISTED" and state.streak == 4
        assert result.outcome == DecisionOutcome.PENDING_APPROVAL
        assert result.ttl_seconds is None
        assert autonomy_service.promote_to_live(session, "agent-1", "read_file", "reviewer-1", True)
        assert state.mode == "LIVE"
        assert state.reviewer_id == "reviewer-1" and state.promoted_at is not None
        result, _, _ = run(session)
        assert result.outcome == DecisionOutcome.ALLOWED
        session.commit()
    with Session(engine) as session:
        state = autonomy_service.get(session, "agent-1", "read_file")
        assert state.mode == "LIVE" and state.reviewer_id == "reviewer-1"
        event_row = session.exec(select(AutonomyPromotionEvent).where(
            AutonomyPromotionEvent.requested_mode == "LIVE"
        )).one()
        assert event_row.approved and event_row.promoted
        assert event_row.evidence_json["threshold"] == 4
        assert len(event_row.evidence_json["decision_ids"]) == 4


def test_declines_and_insufficient_evidence_approvals_are_recorded(engine):
    with Session(engine) as session:
        for _ in range(2):
            run(session)
        assert not autonomy_service.promote_to_live(session, "agent-1", "read_file", "reviewer-1", True)
        assert not autonomy_service.promote_to_live(session, "agent-1", "read_file", "reviewer-2", False)
        session.commit()
    with Session(engine) as session:
        events = session.exec(select(AutonomyPromotionEvent).where(
            AutonomyPromotionEvent.requested_mode == "LIVE"
        )).all()
        assert len(events) == 2
        assert {e.reviewer_id for e in events} == {"reviewer-1", "reviewer-2"}
        assert {e.approved for e in events} == {True, False}
        assert not any(e.promoted for e in events)
        state = autonomy_service.get(session, "agent-1", "read_file")
        assert state.mode == "ASSISTED" and state.reviewer_id is None


@pytest.mark.parametrize("target,expected", [
    ("/outside/notes.txt", DecisionOutcome.DENIED),
    ("/srv/agent/workspace/.env", DecisionOutcome.ESCALATED),
])
def test_violation_resets_streak_and_breaks_promotion_evidence(engine, target, expected):
    with Session(engine) as session:
        run(session)
        result, _, _ = run(session, target=target)
        assert result.outcome == expected
        state = autonomy_service.get(session, "agent-1", "read_file")
        assert state.streak == 0 and state.mode == "SHADOW"
        run(session)
        assert state.streak == 1 and state.mode == "SHADOW"


def test_repeated_record_outcome_calls_do_not_inflate_streak(engine):
    with Session(engine) as session:
        run(session)
        for _ in range(5):
            state = autonomy_service.record_outcome(session, "agent-1", "read_file", violated=False)
        assert state.streak == 1 and state.mode == "SHADOW"
        with pytest.raises(ValueError, match="does not match"):
            autonomy_service.record_outcome(session, "agent-1", "read_file", violated=True)


def test_missing_or_forged_cached_evidence_cannot_qualify(engine):
    with Session(engine) as session:
        with pytest.raises(ValueError, match="without a ledger"):
            autonomy_service.record_outcome(session, "agent-1", "read_file", violated=False)
        state = autonomy_service.get(session, "agent-1", "read_file")
        state.streak = 999
        state.mode = "ASSISTED"
        session.add(state)
        session.flush()
        assert not autonomy_service.promote_to_live(session, "agent-1", "read_file", "reviewer-1", True)
        assert state.mode == "ASSISTED" and state.streak == 0


def test_rollback_removes_decisions_state_changes_and_promotion_event(engine):
    with Session(engine) as session:
        run(session)
        run(session)
        session.rollback()
        assert session.exec(select(DecisionRecord)).all() == []
        assert session.exec(select(AutonomyState)).all() == []
        assert session.exec(select(AutonomyPromotionEvent)).all() == []


def test_additive_migration_creates_promotion_audit_table(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'migration.db'}"
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    database = create_engine(url)
    constraints = inspect(database).get_check_constraints("autonomy_promotion_events")
    assert {c["name"] for c in constraints} == {
        "ck_promotion_previous_mode", "ck_promotion_requested_mode"
    }
    command.downgrade(config, "8696a72fef7b")
    assert "autonomy_promotion_events" not in inspect(database).get_table_names()
    database.dispose()
