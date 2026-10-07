"""Bounded offline EVAL-1..10 harness using migrated temporary databases."""
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from alembic import command
from alembic.config import Config
from sqlalchemy import event
from sqlmodel import Session, create_engine, select
from src.config import PROJECT_ROOT, settings
from src.models.autonomy import AutonomyState
from src.models.decision import DecisionRecord
from src.models.policy import Policy
from src.models.runtime import SimulatedResource
from src.models.request import ActionRequest
from src.services import autonomy_service, escalation_service, executor, policy_promotion, replay_engine, token_issuer
from src.services.orchestrator import evaluate_action
from src.services.policy_loader import seed_policy_artifacts


@contextmanager
def demo_session():
    with TemporaryDirectory(prefix="guarded-demo-") as directory:
        original_url, original_offline = settings.DATABASE_URL, settings.OFFLINE_MODE
        settings.DATABASE_URL = f"sqlite:///{Path(directory) / 'demo.db'}"
        settings.OFFLINE_MODE = True
        engine = None
        try:
            config = Config(str(PROJECT_ROOT / "alembic.ini"))
            config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
            command.upgrade(config, "head")
            engine = create_engine(settings.DATABASE_URL)
            @event.listens_for(engine, "connect")
            def foreign_keys(connection, _):
                connection.execute("PRAGMA foreign_keys=ON")
            with Session(engine) as session:
                seed_policy_artifacts(session)
                session.commit()
                yield session
                session.commit()
        finally:
            if engine is not None:
                engine.dispose()
            settings.DATABASE_URL, settings.OFFLINE_MODE = original_url, original_offline


def propose(session, action="read_file", agent="demo-agent", role="workspace_agent", **overrides):
    values = dict(agent_id=agent, actor_role=role, action_class=action,
                  target_resource="/srv/agent/workspace/demo.txt", environment="development")
    values.update(overrides)
    return evaluate_action(session, ActionRequest(**values))


def earn_live(session, action="create_file", agent="demo-agent", role="workspace_agent", **overrides):
    count = max(settings.PROMOTION_THRESHOLD_ASSISTED, settings.PROMOTION_THRESHOLD_LIVE)
    for _ in range(count):
        propose(session, action, agent, role, **overrides)
    if not autonomy_service.promote_to_live(session, agent, action, "demo-reviewer", True):
        raise AssertionError("recorded clean evidence did not qualify for LIVE")


def run_scenario(number: int) -> dict:
    with demo_session() as session:
        details = {}
        if number == 1:
            earn_live(session)
            result = propose(session, "create_file")
            claims = token_issuer.verify(result.capability_token)
            assert result.decision == "ALLOWED" and claims["exp"] - claims["iat"] == 300
            details = {"decision": result.decision, "ttl_seconds": 300}
        elif number == 2:
            result = propose(session, target_resource="/etc/passwd")
            assert result.decision == "DENIED" and result.capability_token is None
        elif number in {3, 7}:
            if number == 3:
                earn_live(session, "delete_file", role="admin_agent")
                session.add(SimulatedResource(resource="/srv/agent/workspace/demo.txt", value_json={"kind": "file", "content": "synthetic"}))
                held = propose(session, "delete_file", role="admin_agent", environment="production")
                assert held.decision == "ESCALATED"
            else:
                for _ in range(settings.PROMOTION_THRESHOLD_ASSISTED):
                    propose(session, "create_file")
                held = propose(session, "create_file")
                assert held.decision == "PENDING_APPROVAL"
            assert escalation_service.list_pending(session)
            before = session.exec(select(Policy).where(Policy.is_active == True)).one().version
            review = escalation_service.decide(session, held.decision_id, "demo-reviewer", True, "synthetic approval")
            assert review["new_policy_version"] != before
            assert review["evaluation"]["decision"] == "ALLOWED"
            approved = review["evaluation"]
            execution = executor.execute(session, approved["decision_id"], approved["capability_token"])
            assert execution.status == "EXECUTED"
            details = {"held": held.decision, "new_policy_version": review["new_policy_version"]}
        elif number in {4, 5}:
            earn_live(session)
            if number == 5:
                earn_live(session, "update_file")
            allowed = propose(session, "create_file", parameters={"content": "synthetic"})
            execution = executor.execute(session, allowed.decision_id, allowed.capability_token)
            assert execution.status == "EXECUTED"
            if number == 4:
                assert executor.execute(session, allowed.decision_id, allowed.capability_token).status == "REJECTED"
                from jose import jwt
                claims = token_issuer.verify(allowed.capability_token)
                claims["exp"] = 1
                expired = jwt.encode(claims, settings.SECRET_KEY, algorithm="HS256")
                assert token_issuer.verify(expired) is None
                assert executor.execute(session, allowed.decision_id, expired).status == "REJECTED"
            else:
                compensation = executor.rollback(session, execution.execution_id)
                assert compensation.status == "COMPENSATED"
                assert compensation.restored_state == execution.before_state
        elif number == 6:
            candidate = session.exec(select(Policy).where(Policy.is_shadow == True)).one()
            initial = session.exec(select(Policy).where(Policy.is_active == True)).one().policy_hash
            assert not policy_promotion.check_and_promote(session, candidate.policy_hash)
            for _ in range(settings.PROMOTION_THRESHOLD_POLICY):
                propose(session)
            assert session.exec(select(SimulatedResource)).all() == []
            assert policy_promotion.check_and_promote(session, candidate.policy_hash)
            assert session.exec(select(Policy).where(Policy.is_active == True)).one().policy_hash == initial
            assert policy_promotion.promote_candidate(session, candidate.policy_hash, "demo-reviewer", True)
            assert candidate.is_active and not candidate.is_shadow
        elif number == 8:
            samples = [propose(session), propose(session, target_resource="/outside/note"),
                       propose(session, "delete_directory", role="admin_agent")]
            for _ in range(settings.PROMOTION_THRESHOLD_ASSISTED):
                propose(session)
            samples.append(propose(session))
            earn_live(session, "read_file")
            samples.append(propose(session))
            for item in samples:
                assert replay_engine.replay(session, item.decision_id).is_consistent
            details = {"replayed_decisions": len(samples), "outcomes": sorted({item.decision.value for item in samples})}
        elif number == 9:
            result = propose(session, "create_file")
            assert result.decision == "SHADOW_LOGGED" and result.capability_token is None
            assert session.exec(select(SimulatedResource)).all() == []
        elif number == 10:
            for _ in range(settings.PROMOTION_THRESHOLD_ASSISTED):
                propose(session)
            result = propose(session)
            assert result.decision == "PENDING_APPROVAL" and result.capability_token is None
            assert any(p.decision_id == result.decision_id for p in escalation_service.list_pending(session))
        else:
            raise ValueError("scenario number must be 1-10")
        return {"scenario": f"EVAL-{number}", "passed": True, **details}


def run_all_scenarios() -> list[dict]:
    return [run_scenario(number) for number in range(1, 11)]
