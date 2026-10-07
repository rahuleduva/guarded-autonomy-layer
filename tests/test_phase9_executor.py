from jose import jwt
from sqlmodel import select
from src.config import settings
from src.evaluation import propose, earn_live
from src.models.runtime import SimulatedResource, ExecutionRecord
from src.models.token import ConsumedToken
from src.services import executor, token_issuer, escalation_service


def test_tokens_are_bound_expiring_and_single_use(runtime_session):
    session = runtime_session
    earn_live(session)
    allowed = propose(session, "create_file", parameters={"content": "one"})
    claims = token_issuer.verify(allowed.capability_token)
    altered = dict(claims, target_resource="/srv/agent/workspace/other.txt")
    forged = jwt.encode(altered, settings.SECRET_KEY, algorithm="HS256")
    expired = jwt.encode(dict(claims, exp=1), settings.SECRET_KEY, algorithm="HS256")
    for token in (forged, expired, allowed.capability_token + "bad"):
        assert executor.execute(session, allowed.decision_id, token).status == "REJECTED"
    assert not session.exec(select(ConsumedToken)).all()
    result = executor.execute(session, allowed.decision_id, allowed.capability_token)
    assert result.status == "EXECUTED"
    assert executor.execute(session, allowed.decision_id, allowed.capability_token).status == "REJECTED"
    assert len(session.exec(select(ExecutionRecord)).all()) == 1
    assert session.get(SimulatedResource, "/srv/agent/workspace/demo.txt").value_json["content"] == "one"


def test_rollback_requires_its_own_control_decision_and_review(runtime_session):
    session = runtime_session
    earn_live(session)
    allowed = propose(session, "create_file")
    executed = executor.execute(session, allowed.decision_id, allowed.capability_token)
    # Compensation update_file begins SHADOW, so cannot silently run.
    assert executor.rollback(session, executed.execution_id).status == "FAILED"
    assert session.get(SimulatedResource, "/srv/agent/workspace/demo.txt")
    earn_live(session, "update_file")
    rollback = executor.rollback(session, executed.execution_id)
    assert rollback.status == "COMPENSATED" and rollback.restored_state == executed.before_state
    assert session.get(SimulatedResource, "/srv/agent/workspace/demo.txt") is None
    assert executor.rollback(session, executed.execution_id).status == "FAILED"


def test_rollback_will_not_overwrite_newer_content(runtime_session):
    session = runtime_session
    earn_live(session)
    earn_live(session, "update_file")
    created = propose(session, "create_file")
    execution = executor.execute(session, created.decision_id, created.capability_token)
    updated = propose(session, "update_file", parameters={"content": "later"})
    assert executor.execute(session, updated.decision_id, updated.capability_token).status == "EXECUTED"
    assert executor.rollback(session, execution.execution_id).status == "FAILED"
    assert session.get(SimulatedResource, "/srv/agent/workspace/demo.txt").value_json["content"] == "later"


def test_move_destination_scope_and_restore_both_paths(runtime_session):
    session = runtime_session
    earn_live(session, "move_file", parameters={"destination": "/srv/agent/workspace/other.txt"})
    earn_live(session, "update_file")
    path = "/srv/agent/workspace/demo.txt"
    destination = "/srv/agent/workspace/other.txt"
    session.add(SimulatedResource(resource=path, value_json={"kind": "file", "content": "original"}))
    bad = propose(session, "move_file", parameters={"destination": "/outside/file"})
    assert bad.decision == "DENIED" and bad.capability_token is None
    moved = propose(session, "move_file", parameters={"destination": destination})
    execution = executor.execute(session, moved.decision_id, moved.capability_token)
    assert execution.status == "EXECUTED" and session.get(SimulatedResource, path) is None
    assert executor.rollback(session, execution.execution_id).status == "COMPENSATED"
    assert session.get(SimulatedResource, path).value_json["content"] == "original"
    assert session.get(SimulatedResource, destination) is None
