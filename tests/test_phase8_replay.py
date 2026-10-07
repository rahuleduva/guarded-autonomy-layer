import copy
from sqlmodel import select
from src.evaluation import propose, earn_live
from src.models.autonomy import AutonomyState
from src.models.policy import Policy
from src.models.decision import DecisionRecord
from src.services import replay_engine


def test_replay_uses_historical_snapshot_and_does_not_mutate(runtime_session):
    session = runtime_session
    original = propose(session)
    earn_live(session, "read_file")
    session.commit()
    state = session.exec(select(AutonomyState)).one()
    snapshot = state.model_dump()
    count = len(session.exec(select(DecisionRecord)).all())
    for _ in range(3):
        result = replay_engine.replay(session, original.decision_id)
        assert result.is_consistent and result.replayed_outcome == "SHADOW_LOGGED"
    assert state.model_dump() == snapshot
    assert len(session.exec(select(DecisionRecord)).all()) == count
    assert not session.new and not session.dirty and not session.deleted


def test_replay_reports_corrupt_policy(runtime_session):
    result = propose(runtime_session)
    policy = runtime_session.exec(select(Policy).where(Policy.policy_hash == result.policy_hash)).one()
    changed = copy.deepcopy(policy.rules_json)
    changed["version"] = "99.0.0"
    policy.rules_json = changed
    replayed = replay_engine.replay(runtime_session, result.decision_id)
    assert not replayed.is_consistent
    assert "hash" in replayed.discrepancy_reason
