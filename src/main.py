"""Unified FastAPI gateway; each mutation commits before returning authorization."""
from contextlib import asynccontextmanager
import logging
import traceback
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from src.db.database import engine
from src.models.request import ActionRequest, ApprovalDecision, EvaluationResponse, NaturalLanguageRequest
from src.services import autonomy_service, escalation_service, executor, llm_parser, policy_promotion, replay_engine
from src.services.orchestrator import evaluate_action
from src.services.policy_loader import seed_policy_artifacts
from src.services.policy_store import active
from src.config import settings

logger = logging.getLogger(__name__)


class ControlPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PolicyPromotion(ControlPayload):
    candidate_policy_hash: str
    reviewer_id: str
    approve: bool


class AgentPromotion(ControlPayload):
    agent_id: str
    action_class: str
    reviewer_id: str
    approve: bool


class ExecutePayload(ControlPayload):
    decision_id: str
    token: str


def create_app(database_engine=None) -> FastAPI:
    database_engine = database_engine if database_engine is not None else engine

    @asynccontextmanager
    async def lifespan(_app):
        # Alembic owns table creation; seed only after migrations are applied.
        with Session(database_engine) as session:
            seed_policy_artifacts(session)
            session.commit()
        yield

    application = FastAPI(title="Guarded Action & Autonomy Control Layer", lifespan=lifespan)

    def session_dependency():
        with Session(database_engine) as session:
            yield session

    def transaction(session, operation):
        phase = "operation"
        try:
            result = operation()
            phase = "commit"
            session.commit()
            return result
        except IntegrityError:
            session.rollback()
            raise HTTPException(409, "duplicate request or conflicting database state") from None
        except llm_parser.OnlineActionParseError as exc:
            session.rollback()
            raise HTTPException(502, str(exc)) from None
        except ValueError as exc:
            session.rollback()
            raise HTTPException(400, str(exc)) from None
        except Exception as exc:
            # Frame locations identify the failure without logging SQL parameters,
            # request bodies, connection strings, or capability tokens.
            logger.error(
                "Transaction failed: operation=%s phase=%s exception=%s\n%s",
                operation.__qualname__, phase, type(exc).__name__,
                "".join(traceback.format_list(traceback.extract_tb(exc.__traceback__))),
            )
            session.rollback()
            if settings.DEV_DIAGNOSTICS:
                raise
            raise HTTPException(503, "operation aborted; audit or execution could not be committed") from None

    @application.get("/api/v1/health")
    def health():
        return {"status": "ok"}

    @application.post("/api/v1/actions/evaluate", response_model=EvaluationResponse)
    def evaluate(request: ActionRequest, session: Session = Depends(session_dependency)):
        return transaction(session, lambda: evaluate_action(session, request))

    @application.post("/api/v1/actions/evaluate-prompt", response_model=EvaluationResponse)
    def evaluate_prompt(request: NaturalLanguageRequest, session: Session = Depends(session_dependency)):
        return transaction(session, lambda: evaluate_action(session, llm_parser.parse(request)))

    @application.post("/api/v1/control/approve/{decision_id}")
    def approve(decision_id: str, request: ApprovalDecision, session: Session = Depends(session_dependency)):
        if request.decision_id != decision_id:
            raise HTTPException(400, "path and payload decision IDs must match")
        return transaction(session, lambda: escalation_service.decide(
            session, decision_id, request.reviewer_id, request.approve, request.note))

    @application.post("/api/v1/control/promote-policy")
    def promote_policy(request: PolicyPromotion, session: Session = Depends(session_dependency)):
        def operation():
            promoted = policy_promotion.promote_candidate(session, request.candidate_policy_hash,
                                                          request.reviewer_id, request.approve)
            current = active(session)
            return {"promoted": promoted, "active_policy_hash": current.policy_hash, "active_policy_version": current.version}
        return transaction(session, operation)

    @application.post("/api/v1/control/promote-agent")
    def promote_agent(request: AgentPromotion, session: Session = Depends(session_dependency)):
        def operation():
            promoted = autonomy_service.promote_to_live(session, request.agent_id, request.action_class,
                                                       request.reviewer_id, request.approve)
            state = autonomy_service.get(session, request.agent_id, request.action_class)
            return {"promoted": promoted, "autonomy_level": state.mode, "streak": state.streak}
        return transaction(session, operation)

    @application.post("/api/v1/actions/execute")
    def execute(request: ExecutePayload, session: Session = Depends(session_dependency)):
        return transaction(session, lambda: executor.execute(session, request.decision_id, request.token))

    @application.post("/api/v1/actions/rollback/{execution_id}")
    def rollback(execution_id: str, session: Session = Depends(session_dependency)):
        return transaction(session, lambda: executor.rollback(session, execution_id))

    @application.get("/api/v1/escalations")
    def escalations(session: Session = Depends(session_dependency)):
        return escalation_service.list_pending(session)

    @application.get("/api/v1/replay/{decision_id}")
    def replay(decision_id: str, session: Session = Depends(session_dependency)):
        try:
            return replay_engine.replay(session, decision_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None

    return application


app = create_app()
