"""Unified FastAPI gateway; each mutation commits before returning authorization."""
from contextlib import asynccontextmanager
import logging
import traceback
from time import monotonic
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session
from starlette.concurrency import run_in_threadpool

from src.db.database import engine
from src.models.request import (ActionRequest, ApprovalDecision, EvaluationResponse, NaturalLanguageRequest,
                                ClarificationAnswer, ClarificationResponse,
                                ParserRejectionResponse, ParserFailureResponse)
from src.models.human_review import HumanReviewResponse
from src.services import clarification_service
from src.services import autonomy_service, escalation_service, executor, llm_parser, monitoring, policy_promotion, replay_engine, request_analysis
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
        from src.services.llm_routing import validate_runtime_config
        validate_runtime_config()
        # Alembic owns table creation; seed only after migrations are applied.
        with Session(database_engine) as session:
            seed_policy_artifacts(session)
            session.commit()
        yield

    application = FastAPI(title="Guarded Action & Autonomy Control Layer", lifespan=lifespan)

    @application.exception_handler(RequestValidationError)
    async def record_request_validation(request: Request, exc: RequestValidationError):
        if (request.url.path == "/api/v1/actions/evaluate-prompt"
                or request.url.path.startswith("/api/v1/actions/clarifications/")
                or request.url.path.startswith("/api/v1/control/approve/")):
            errors = {}
            for issue in exc.errors():
                field = ".".join(map(str, issue["loc"]))
                errors.setdefault(field, []).append({"code": issue["type"], "message": issue["msg"]})
            trace = (None if request.url.path.startswith("/api/v1/control/approve/")
                     else {"request_body": jsonable_encoder(exc.body)})
            monitoring.record("request_validation", "rejected", http_status=422,
                              validation_errors=errors, parser_trace=trace)
        return await request_validation_exception_handler(request, exc)

    @application.middleware("http")
    async def monitor_evaluations(request: Request, call_next):
        if (request.url.path not in {"/api/v1/actions/evaluate", "/api/v1/actions/evaluate-prompt"}
                and not request.url.path.startswith("/api/v1/actions/clarifications/")
                and not request.url.path.startswith("/api/v1/control/approve/")
                and not request.url.path.startswith("/api/v1/escalations/")
                ):
            return await call_next(request)
        attempt, token = monitoring.begin()
        started = monotonic()
        response = None
        try:
            response = await call_next(request)
            response.headers["X-Correlation-ID"] = attempt.attempt_id
            return response
        except Exception as exc:
            monitoring.record("request", "failed", duration_ms=(monotonic() - started) * 1000,
                              error_type=type(exc).__name__, http_status=500)
            raise
        finally:
            route = request.scope.get("route")
            attempt.route = route.path if route else "<unmatched>"
            if response is not None:
                monitoring.record("request", "completed" if response.status_code < 400 else "failed",
                                  duration_ms=(monotonic() - started) * 1000,
                                  error_type=f"HTTP_{response.status_code}" if response.status_code >= 400 else None,
                                  http_status=response.status_code)
            try:
                await run_in_threadpool(monitoring.persist, attempt, database_engine)
            finally:
                monitoring.end(token)

    def session_dependency():
        with Session(database_engine) as session:
            yield session

    def transaction(session, operation):
        phase = "operation"
        try:
            result = operation()
            phase = "commit"
            with monitoring.span("commit"):
                session.commit()
            return result
        except IntegrityError:
            session.rollback()
            raise HTTPException(409, "duplicate request or conflicting database state") from None
        except HTTPException:
            session.rollback()
            raise
        except llm_parser.OnlineActionParseError as exc:
            session.rollback()
            raise HTTPException(502, str(exc)) from None
        except escalation_service.ReviewConflictError as exc:
            session.rollback()
            raise HTTPException(409, str(exc)) from None
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

    def parse_for_evaluation(request):
        def operation():
            from src.services.llm_routing import resolve_purpose
            profile = resolve_purpose('request_parser')[1][0]
            provider, model = profile.provider, profile.model
            with monitoring.span("parser", provider=provider, model=model) as event:
                try:
                    parsed = llm_parser.parse(request)
                except (llm_parser.UnverifiedActionError,
                        llm_parser.OnlineActionParseError) as exc:
                    analysis = getattr(exc, "analysis", None)
                    if analysis is not None:
                        monitoring.record("analyzer", analysis.get("status", "unavailable"),
                                          provider=analysis.get("provider"), model=analysis.get("model"),
                                          error_type=analysis.get("error"), http_status=analysis.get("http_status"))
                    raise
                analysis = parsed._semantic_analysis
                provenance = parsed.parameters.get('parser_provenance') or {}
                actual = provenance.get('source')
                if isinstance(actual, str) and ':' in actual:
                    actual_provider, actual_model = actual.split(':', 1)
                    event.update(provider=actual_provider, model=actual_model)
            if analysis is not None:
                monitoring.record("analyzer", analysis.get("status", "unavailable"),
                                  provider=analysis.get("provider"), model=analysis.get("model"),
                                  error_type=analysis.get("error"), http_status=analysis.get("http_status"))
            return parsed
        return operation()

    @application.post("/api/v1/actions/evaluate-prompt", response_model=EvaluationResponse | ClarificationResponse,
                      responses={202: {"model": ClarificationResponse},
                                 400: {"model": ParserRejectionResponse},
                                 502: {"model": ParserFailureResponse}})
    def evaluate_prompt(request: NaturalLanguageRequest, session: Session = Depends(session_dependency)):
        return transaction(session, lambda: clarification_service.submit(
            session, request, parse_for_evaluation))

    @application.post("/api/v1/actions/clarifications/{interpretation_id}",
                      response_model=EvaluationResponse | ClarificationResponse,
                      responses={202: {"model": ClarificationResponse},
                                 400: {"model": ParserRejectionResponse},
                                 502: {"model": ParserFailureResponse}})
    def answer_clarification(interpretation_id: str, answer: ClarificationAnswer,
                             session: Session = Depends(session_dependency)):
        return transaction(session, lambda: clarification_service.resume(
            session, interpretation_id, answer,
            parse_for_evaluation))

    @application.get("/api/v1/actions/clarifications/{interpretation_id}")
    def get_interpretation(interpretation_id: str, agent_id: str, actor_role: str,
                           session: Session = Depends(session_dependency)):
        from src.models.clarification import RequestInterpretation
        row = session.get(RequestInterpretation, interpretation_id)
        if row is None:
            raise HTTPException(404, "clarification request not found")
        if (row.agent_id, row.actor_role) != (agent_id, actor_role):
            raise HTTPException(403, "caller identity differs from the original request")
        return row

    @application.post("/api/v1/control/approve/{decision_id}")
    def approve(decision_id: str, request: ApprovalDecision, session: Session = Depends(session_dependency)) -> HumanReviewResponse:
        result = transaction(session, lambda: escalation_service.decide(session, decision_id, request))
        monitoring.record("human_review", "accepted", decision_id=decision_id,
                          parser_trace={"review_id": result.review_id,
                                        "review_status": result.status,
                                        "authorization_status": result.authorization_status,
                                        "authorization_decision_id": result.authorization_decision_id})
        return result

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

    @application.post("/api/v1/escalations/{decision_id}/prepare")
    def prepare_escalation(decision_id: str, session: Session = Depends(session_dependency)):
        return transaction(session, lambda: escalation_service.prepare_existing(session, decision_id))

    @application.get("/api/v1/replay/{decision_id}")
    def replay(decision_id: str, session: Session = Depends(session_dependency)):
        try:
            return replay_engine.replay(session, decision_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None

    return application


app = create_app()
