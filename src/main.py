"""Unified FastAPI gateway; each mutation commits before returning authorization."""
from contextlib import asynccontextmanager
import logging
import traceback
from time import monotonic
from fastapi import Depends, FastAPI, HTTPException, Request, Header, Response
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader, HTTPBearer
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError
from sqlalchemy import inspect, func
from datetime import datetime, timezone, timedelta
from sqlmodel import Session
from starlette.concurrency import run_in_threadpool

from src.db.database import engine
from src.models.request import (ActionRequest, ApprovalDecision, EvaluationResponse, NaturalLanguageRequest,
                                ClarificationAnswer, ClarificationResponse,
                                ParserRejectionResponse, ParserFailureResponse)
from src.models.workflow import WorkflowProgress
from src.models.human_review import HumanReviewResponse
from src.models.request import ExecutionResult, RollbackResult, AgentReviewStatus
from src.services import clarification_service
from src.services import autonomy_service, escalation_service, executor, llm_parser, monitoring, policy_promotion, replay_engine, request_analysis
from src.services.orchestrator import evaluate_action
from src.services import workflow, workflow_flows
from src.services import request_parsing, workflow_submission
from src.services.policy_loader import seed_policy_artifacts
from src.services.policy_store import active
from src.config import settings
from src.services import authentication as auth
from src.models.auth import LoginRequest, LoginResponse, CallerIdentity

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
        auth.validate_config()
        from src.services.llm_routing import validate_runtime_config
        validate_runtime_config()
        with database_engine.connect() as connection:
            if connection.dialect.name != 'postgresql':
                raise RuntimeError('the application requires the configured PostgreSQL database')
            inspector = inspect(connection)
            for table in ('auth_principals', 'auth_credentials', 'auth_events'):
                if not inspector.has_table(table):
                    raise RuntimeError('authentication schema is missing; generate, review, and apply the migration')
            from src.db.workflow_schema import validate_workflow_schema
            validate_workflow_schema(connection)
            for table, name, required_values in (
                ('escalation_queue', 'ck_escalation_status', ('PREPARING',)),
                ('request_interpretations', 'ck_interpretation_status', ('processing', 'processing_failed')),
            ):
                constraint = next((item for item in inspector.get_check_constraints(table)
                                   if item['name'] == name), None)
                if constraint is None or any("'" + value + "'" not in constraint['sqltext'] for value in required_values):
                    raise RuntimeError(f'{name} is outdated; include its new processing states in the pending migration')
        # Alembic owns table creation; seed only after migrations are applied.
        with Session(database_engine) as session:
            seed_policy_artifacts(session)
            session.commit()
        yield

    api_key_scheme = APIKeyHeader(name='X-API-Key', auto_error=False)
    bearer_scheme = HTTPBearer(auto_error=False)

    def documented_auth(api_key=Depends(api_key_scheme), bearer=Depends(bearer_scheme)):
        # Verification is performed by middleware before body validation.
        return auth.current.get()

    application = FastAPI(title="Guarded Action & Autonomy Control Layer", lifespan=lifespan,
                          dependencies=[Depends(documented_auth)])

    def audit_unavailable(correlation_id):
        return JSONResponse(status_code=503,
            content={'detail': 'authentication audit temporarily unavailable', 'correlation_id': correlation_id},
            headers={'X-Correlation-ID': correlation_id, 'Cache-Control': 'no-store'})

    @application.exception_handler(RequestValidationError)
    async def record_request_validation(request: Request, exc: RequestValidationError):
        if request.url.path == '/api/v1/auth/login':
            from uuid import uuid4
            correlation = str(uuid4())
            recorded = await run_in_threadpool(auth.audit, database_engine, None, 'denied', 'login_validation',
                                               correlation_id=correlation, route='/api/v1/auth/login')
            if not recorded:
                return audit_unavailable(correlation)
            return JSONResponse(status_code=422, content={'detail': [
                {'loc': list(issue['loc']), 'type': issue['type'], 'msg': 'invalid login input'}
                for issue in exc.errors()]},
                headers={'X-Correlation-ID': correlation, 'Cache-Control': 'no-store'})
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
        if (request.url.path not in {"/api/v1/actions/evaluate", "/api/v1/actions/evaluate-prompt", "/api/v1/actions/execute"}
                and not request.url.path.startswith("/api/v1/actions/clarifications/")
                and not request.url.path.startswith("/api/v1/control/")
                and not request.url.path.startswith("/api/v1/actions/rollback/")
                and not request.url.path.startswith("/api/v1/escalations/")
                ):
            return await call_next(request)
        attempt, token = monitoring.begin()
        if getattr(request.state, 'authenticated_caller', None):
            attempt.attempt_id = request.state.authenticated_caller.correlation_id
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

    @application.middleware('http')
    async def authenticate_request(request: Request, call_next):
        path = request.url.path
        if path in {'/api/v1/health', '/api/v1/auth/login'}:
            return await call_next(request)
        from uuid import uuid4
        correlation = str(uuid4())
        caller, context_token = None, None
        try:
            if len(request.headers.getlist('authorization')) > 1 or len(request.headers.getlist('x-api-key')) > 1:
                raise HTTPException(400, 'duplicate authentication headers')
            caller = await run_in_threadpool(auth.authenticate, database_engine,
                request.headers.get('authorization'), request.headers.get('x-api-key'), correlation, path)
            # Permission checks precede body validation and workflow/provider work.
            category = {
                '/api/v1/actions/evaluate': 'evaluate', '/api/v1/actions/evaluate-prompt': 'interpret',
                '/api/v1/actions/execute': 'execute', '/api/v1/control/promote-policy': 'promote_policy',
                '/api/v1/control/promote-agent': 'promote_agent', '/api/v1/escalations': 'evidence',
                '/api/v1/operations/diagnostics': 'evidence',
            }.get(path)
            if path.startswith('/api/v1/control/approve/'):
                category = 'review'
            elif path.startswith('/api/v1/actions/clarifications/'):
                category = 'clarify' if request.method == 'POST' else 'read_own'
            elif path.startswith('/api/v1/actions/rollback/'):
                category = 'rollback'
            elif path.startswith(('/api/v1/actions/reviews/', '/api/v1/actions/authorizations/')):
                category = 'read_own'
            elif path.startswith('/api/v1/escalations/'):
                category = 'prepare_review'
            elif path.startswith('/api/v1/replay/'):
                category = 'evidence'
            if category:
                auth.require(caller, category)
            context_token = auth.current.set(caller)
            request.state.authenticated_caller = caller
            response = await call_next(request)
            if response.status_code in {401, 403, 404}:
                recorded = await run_in_threadpool(auth.audit, database_engine, caller, 'denied',
                                                   'HTTP_' + str(response.status_code))
                if not recorded:
                    return audit_unavailable(correlation)
            elif request.method == 'GET' and response.status_code < 400:
                await run_in_threadpool(auth.audit, database_engine, caller, 'accepted', 'read')
            response.headers.setdefault('X-Correlation-ID', correlation)
            return response
        except HTTPException as exc:
            recorded = await run_in_threadpool(auth.audit, database_engine, caller, 'denied', 'HTTP_' + str(exc.status_code),
                                               correlation_id=correlation, route=path)
            if not recorded:
                return audit_unavailable(correlation)
            return JSONResponse(status_code=exc.status_code, content={'detail': exc.detail},
                                headers={**(exc.headers or {}), 'X-Correlation-ID': correlation})
        finally:
            if context_token is not None:
                auth.current.reset(context_token)

    def session_dependency():
        with Session(database_engine) as session:
            yield session

    @application.post('/api/v1/auth/login', response_model=LoginResponse, openapi_extra={'security': []})
    def login(request: LoginRequest, http_request: Request):
        from uuid import uuid4
        correlation = str(uuid4())
        try:
            result = auth.login(database_engine, request,
                http_request.client.host if http_request.client else '<unknown>', correlation)
        except HTTPException as exc:
            recorded = auth.audit(database_engine, None, 'denied', 'login_HTTP_' + str(exc.status_code),
                                  correlation_id=correlation, route='/api/v1/auth/login')
            if not recorded:
                return audit_unavailable(correlation)
            exc.headers = {**(exc.headers or {}), 'X-Correlation-ID': correlation, 'Cache-Control': 'no-store'}
            raise
        return JSONResponse(content=result.model_dump(mode='json'),
                            headers={'Cache-Control': 'no-store', 'X-Correlation-ID': correlation})

    @application.get('/api/v1/auth/me', response_model=CallerIdentity)
    def who_am_i(session: Session = Depends(session_dependency, scope='function')):
        caller = auth.refresh(session, auth.current.get())
        return CallerIdentity(principal_id=caller.principal_id, kind=caller.kind,
                              agent_id=caller.agent_id, actor_role=caller.actor_role,
                              permissions=sorted(caller.permissions))

    def operation_response(operation):
        # Each workflow stage owns its own short transaction. This boundary
        # translates exceptions only; it never opens a database transaction.
        try:
            return operation()
        except IntegrityError:
            raise HTTPException(409, "duplicate request or conflicting database state") from None
        except HTTPException:
            raise
        except llm_parser.OnlineActionParseError as exc:
            raise HTTPException(502, str(exc)) from None
        except escalation_service.ReviewConflictError as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        except Exception as exc:
            logger.error(
                "Workflow failed: operation=%s exception=%s\n%s",
                operation.__qualname__, type(exc).__name__,
                "".join(traceback.format_list(traceback.extract_tb(exc.__traceback__))),
            )
            if settings.DEV_DIAGNOSTICS:
                raise
            raise HTTPException(503, "operation interrupted; retry with the same idempotency key") from None

    def run_or_submit(kind, scope, key, payload, work, normalized_request=None):
        # The flag controls new jobs. Previously accepted jobs keep their mode
        # and remain queued/resumable while workers drain them.
        response = workflow_submission.submit(database_engine, kind, scope, key, payload,
            normalized_request=normalized_request, allow_new=settings.BACKGROUND_WORKFLOWS_ENABLED)
        if response is not None:
            return response
        return workflow.run(database_engine, kind, scope, key, payload, work)

    @application.get("/api/v1/health", openapi_extra={'security': []})
    def health():
        return {"status": "ok"}

    @application.post("/api/v1/actions/evaluate", response_model=EvaluationResponse,
                      responses={202: {"model": WorkflowProgress}})
    def evaluate(request: ActionRequest,
                 idempotency_key: str | None = Header(default=None)):
        return operation_response(lambda: run_or_submit('evaluate', workflow.digest([request.agent_id, request.actor_role]),
            (idempotency_key if idempotency_key is not None else request.request_id), request.model_dump(mode='json', exclude_unset=True),
            lambda op: workflow_flows.evaluate(op, request), normalized_request=request.model_dump(mode='json')))

    @application.post("/api/v1/actions/evaluate-prompt", response_model=EvaluationResponse | ClarificationResponse,
                      responses={202: {"model": ClarificationResponse | WorkflowProgress},
                                 400: {"model": ParserRejectionResponse},
                                 502: {"model": ParserFailureResponse}})
    def evaluate_prompt(request: NaturalLanguageRequest,
                        idempotency_key: str | None = Header(default=None)):
        return operation_response(lambda: run_or_submit('interpret', workflow.digest([request.agent_id, request.actor_role]),
            (idempotency_key if idempotency_key is not None else request.request_id), request.model_dump(mode='json', exclude_unset=True),
            lambda op: workflow_flows.interpret(op, request, request_parsing.parse_for_evaluation),
            normalized_request=request.model_dump(mode='json')))

    @application.post("/api/v1/actions/clarifications/{interpretation_id}",
                      response_model=EvaluationResponse | ClarificationResponse,
                      responses={202: {"model": ClarificationResponse | WorkflowProgress},
                                 400: {"model": ParserRejectionResponse},
                                 502: {"model": ParserFailureResponse}})
    def answer_clarification(interpretation_id: str, answer: ClarificationAnswer,
                             idempotency_key: str | None = Header(default=None)):
        source = NaturalLanguageRequest(agent_id=answer.agent_id, actor_role=answer.actor_role,
                                        raw_prompt=answer.clarified_prompt)
        return operation_response(lambda: run_or_submit('clarify', interpretation_id,
            (idempotency_key if idempotency_key is not None else str(answer.revision)), answer.model_dump(mode='json'),
            lambda op: workflow_flows.interpret(op, source, request_parsing.parse_for_evaluation,
                interpretation_id=interpretation_id, answer=answer), normalized_request=source.model_dump(mode='json')))

    @application.get("/api/v1/actions/clarifications/{interpretation_id}")
    def get_interpretation(interpretation_id: str, agent_id: str, actor_role: str,
                           session: Session = Depends(session_dependency, scope='function')):
        caller = auth.current.get()
        auth.authorize_read(session, caller, 'clarification', interpretation_id)
        if (agent_id, actor_role) != (caller.agent_id, caller.actor_role):
            raise HTTPException(403, 'submitted identity differs from authenticated caller')
        from src.models.clarification import RequestInterpretation
        row = session.get(RequestInterpretation, interpretation_id)
        if row is None:
            raise HTTPException(404, "clarification request not found")
        if (row.agent_id, row.actor_role) != (agent_id, actor_role):
            raise HTTPException(403, "caller identity differs from the original request")
        return row

    @application.post("/api/v1/control/approve/{decision_id}", response_model=HumanReviewResponse,
                      responses={202: {"model": WorkflowProgress}})
    def approve(decision_id: str, request: ApprovalDecision,
                idempotency_key: str | None = Header(default=None)):
        def record_review(db, checkpoint):
            reviewed = escalation_service.decide(db, decision_id, request)
            checkpoint['review_id'] = reviewed.review_id
            return reviewed
        result = operation_response(lambda: workflow.run(database_engine, 'review', decision_id,
            (idempotency_key if idempotency_key is not None else workflow.digest(request)), request.model_dump(mode='json'),
            lambda op: op.transaction('completed', lambda db, checkpoint:
                record_review(db, checkpoint), complete=True)))
        if result.status_code == 200:
            import json
            reviewed = json.loads(result.body)
            monitoring.record("human_review", "accepted", decision_id=decision_id,
                              parser_trace={"review_id": reviewed['review_id'],
                                            "review_status": reviewed['status'],
                                            "authorization_status": reviewed['authorization_status'],
                                            "authorization_decision_id": reviewed['authorization_decision_id']})
            # The agent retrieves the original encrypted capability through its
            # ownership-checked authorization endpoint; reviewers never receive it.
            result = workflow.public_response('review', result)
        return result

    @application.get('/api/v1/actions/authorizations/{decision_id}', response_model=EvaluationResponse)
    def authorization_result(decision_id: str, response: Response,
                             session: Session = Depends(session_dependency, scope='function')):
        response.headers['Cache-Control'] = 'no-store'
        from sqlmodel import select
        from src.models.human_review import HumanReviewRecord
        from src.services.agent_review import evaluation_for_review
        caller = auth.refresh(session, auth.current.get())
        auth.require(caller, 'read_own')
        auth.own_decision(session, caller, decision_id)
        review = session.exec(select(HumanReviewRecord).where(
            HumanReviewRecord.authorization_decision_id == decision_id)).one_or_none()
        if review is None:
            raise HTTPException(404, 'authorization result not found')
        return evaluation_for_review(session, caller, review)

    @application.get('/api/v1/actions/reviews/{original_decision_id}', response_model=AgentReviewStatus)
    def agent_review_status(original_decision_id: str, response: Response,
                            session: Session = Depends(session_dependency, scope='function')):
        response.headers['Cache-Control'] = 'no-store'
        from src.services.agent_review import status
        return status(session, auth.current.get(), original_decision_id)

    @application.post("/api/v1/control/promote-policy", responses={202: {"model": WorkflowProgress}})
    def promote_policy(request: PolicyPromotion,
                       idempotency_key: str = Header(..., min_length=1, max_length=200)):
        def operation(db, checkpoint):
            promoted = policy_promotion.promote_candidate(db, request.candidate_policy_hash,
                                                          request.reviewer_id, request.approve)
            current = active(db)
            return {"promoted": promoted, "active_policy_hash": current.policy_hash, "active_policy_version": current.version}
        return operation_response(lambda: workflow.run(database_engine, 'promote_policy', request.candidate_policy_hash,
            idempotency_key, request.model_dump(mode='json'),
            lambda op: op.transaction('completed', operation, complete=True)))

    @application.post("/api/v1/control/promote-agent", responses={202: {"model": WorkflowProgress}})
    def promote_agent(request: AgentPromotion,
                      idempotency_key: str = Header(..., min_length=1, max_length=200)):
        def operation(db, checkpoint):
            promoted = autonomy_service.promote_to_live(db, request.agent_id, request.action_class,
                                                       request.reviewer_id, request.approve)
            state = autonomy_service.get(db, request.agent_id, request.action_class)
            return {"promoted": promoted, "autonomy_level": state.mode, "streak": state.streak}
        return operation_response(lambda: workflow.run(database_engine, 'promote_agent', request.agent_id + ':' + request.action_class,
            idempotency_key, request.model_dump(mode='json'),
            lambda op: op.transaction('completed', operation, complete=True)))

    @application.post("/api/v1/actions/execute", response_model=ExecutionResult,
                      responses={202: {"model": WorkflowProgress}})
    def execute(request: ExecutePayload,
                idempotency_key: str | None = Header(default=None)):
        return operation_response(lambda: workflow.run(database_engine, 'execute', request.decision_id,
            (idempotency_key if idempotency_key is not None else workflow.digest(request)), request.model_dump(mode='json'),
            lambda op: op.transaction('completed', lambda db, checkpoint:
                executor.execute(db, request.decision_id, request.token), complete=True)))

    @application.post("/api/v1/actions/rollback/{execution_id}", response_model=RollbackResult,
                      responses={202: {"model": WorkflowProgress}})
    def rollback(execution_id: str,
                 idempotency_key: str | None = Header(default=None)):
        return operation_response(lambda: run_or_submit('rollback', execution_id,
            (idempotency_key if idempotency_key is not None else execution_id), {'execution_id': execution_id},
            lambda op: workflow_flows.rollback(op, execution_id)))

    @application.get("/api/v1/escalations")
    def escalations(session: Session = Depends(session_dependency, scope='function')):
        auth.authorize_read(session, auth.current.get(), 'evidence')
        return escalation_service.list_pending(session)

    @application.post("/api/v1/escalations/{decision_id}/prepare", responses={202: {"model": WorkflowProgress}})
    def prepare_escalation(decision_id: str,
                           idempotency_key: str | None = Header(default=None)):
        return operation_response(lambda: run_or_submit('prepare_review', decision_id,
            (idempotency_key if idempotency_key is not None else decision_id), {'decision_id': decision_id},
            lambda op: workflow_flows.prepare(op, decision_id)))

    @application.get('/api/v1/operations/diagnostics')
    def operation_diagnostics(session: Session = Depends(session_dependency, scope='function')):
        """Authenticated, database-only background queue health summary."""
        auth.authorize_read(session, auth.current.get(), 'evidence')
        from src.models.workflow import WorkflowOperation
        from src.models.workflow_dispatch import WorkflowDispatch
        from src.models.monitoring import RequestAttemptEvent
        current_time = datetime.now(timezone.utc)
        counts = {status: count for status, count in session.exec(
            select(WorkflowOperation.status, func.count()).where(
                WorkflowOperation.execution_mode == 'background')
            .group_by(WorkflowOperation.status)).all()}
        oldest_queued = session.exec(select(func.min(WorkflowOperation.created_at)).where(
            WorkflowOperation.execution_mode == 'background',
            WorkflowOperation.status.in_(['QUEUED', 'RETRY_WAIT']))).one()
        oldest_pending = session.exec(select(func.min(WorkflowDispatch.available_at)).where(
            WorkflowDispatch.status == 'PENDING')).one()
        heartbeat_info = {}
        for stage in ('dispatcher_heartbeat', 'worker_heartbeat'):
            event = session.exec(select(RequestAttemptEvent).where(
                RequestAttemptEvent.stage == stage).order_by(
                    RequestAttemptEvent.created_at.desc()).limit(1)).first()
            age = max(0, (current_time - event.created_at.replace(tzinfo=timezone.utc)
                          if event and event.created_at.tzinfo is None else
                          current_time - event.created_at).total_seconds()) if event else None
            heartbeat_info[stage] = {'last_seen_at': event.created_at if event else None,
                                     'age_seconds': age, 'stale': age is None or age > 120}
        failures_24h = session.exec(select(func.count()).select_from(RequestAttemptEvent).where(
            RequestAttemptEvent.status == 'failed',
            RequestAttemptEvent.created_at >= current_time - timedelta(hours=24))).one()
        def age_seconds(value):
            if value is None:
                return None
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return max(0, int((current_time - value).total_seconds()))
        return {'background_workflows_enabled': settings.BACKGROUND_WORKFLOWS_ENABLED,
            'operation_counts': counts,
            'oldest_queued_age_seconds': age_seconds(oldest_queued),
            'oldest_pending_dispatch_age_seconds': age_seconds(oldest_pending),
            'process_heartbeats': heartbeat_info,
            'failed_attempt_events_last_24h': failures_24h,
            'staleness_threshold_seconds': 120,
            'observed_at': current_time}

    @application.get('/api/v1/operations/{operation_id}')
    def operation_status(operation_id: str, response: Response,
                         session: Session = Depends(session_dependency, scope='function')):
        response.headers['Cache-Control'] = 'no-store'
        row = auth.authorize_read(session, auth.current.get(), 'operation', operation_id)
        if row.caller_principal_id != auth.current.get().principal_id:
            return {key: getattr(row, key) for key in ('operation_id', 'kind', 'status', 'stage', 'created_at', 'updated_at')}
        return workflow.status(session, operation_id)

    @application.get('/api/v1/operations/{operation_id}/result')
    def operation_result(operation_id: str, response: Response,
                         session: Session = Depends(session_dependency, scope='function')):
        response.headers['Cache-Control'] = 'no-store'
        caller = auth.current.get()
        row = auth.authorize_read(session, caller, 'operation', operation_id)
        if row.caller_principal_id != caller.principal_id:
            raise HTTPException(404, 'operation not found')
        return workflow.result(session, row)

    @application.get("/api/v1/replay/{decision_id}")
    def replay(decision_id: str, session: Session = Depends(session_dependency, scope='function')):
        auth.authorize_read(session, auth.current.get(), 'evidence')
        try:
            return replay_engine.replay(session, decision_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None

    original_openapi = application.openapi

    def authentication_openapi():
        schema = original_openapi()
        endpoint_categories = {
            'evaluate': 'evaluate', 'evaluate_prompt': 'interpret', 'answer_clarification': 'clarify',
            'get_interpretation': 'read_own', 'execute': 'execute', 'rollback': 'rollback',
            'approve': 'review', 'promote_policy': 'promote_policy', 'promote_agent': 'promote_agent',
            'escalations': 'evidence', 'prepare_escalation': 'prepare_review', 'replay': 'evidence',
            'authorization_result': 'read_own', 'agent_review_status': 'read_own',
            'operation_result': 'read_own',
            'operation_diagnostics': 'evidence',
        }
        for route in application.routes:
            endpoint = getattr(route, 'endpoint', None)
            category = endpoint_categories.get(getattr(endpoint, '__name__', ''))
            for method in getattr(route, 'methods', ()):
                operation = schema.get('paths', {}).get(route.path, {}).get(method.lower())
                if operation is None:
                    continue
                if route.path in {'/api/v1/health', '/api/v1/auth/login'}:
                    operation['security'] = []
                if category:
                    principal_kind, permissions = auth.ROUTE_PERMISSIONS[category]
                    operation['x-principal-kind'] = principal_kind
                    operation['x-required-permissions'] = sorted(permissions)
                elif route.path == '/api/v1/operations/{operation_id}':
                    operation['description'] = 'Original caller with current operation permission, or human evidence:read with safe status only.'
        return schema

    application.openapi = authentication_openapi
    return application


app = create_app()
