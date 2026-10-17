"""Request-scoped stage events with a separate best-effort persistence path.

Parser events include the original prompt, accumulated messages, structured
response, validation errors and correction message for each attempt.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
import logging
from time import monotonic
from typing import Any
from uuid import uuid4

from sqlmodel import Session

from src.models.monitoring import RequestAttemptEvent

logger = logging.getLogger(__name__)


@dataclass
class Attempt:
    attempt_id: str = field(default_factory=lambda: str(uuid4()))
    route: str = "<unmatched>"
    events: list[dict[str, Any]] = field(default_factory=list)
    decision_id: str | None = None
    operation_id: str | None = None
    correlation_id: str | None = None
    pending_decision_id: str | None = None
    persisted_count: int = 0
    decision_link_version: int = 0
    persisted_link_version: int = 0
    persistence_engine: Any = field(default=None, repr=False)


_current: ContextVar[Attempt | None] = ContextVar("request_attempt", default=None)


def begin() -> tuple[Attempt, Any]:
    attempt = Attempt()
    return attempt, _current.set(attempt)


def end(token: Any) -> None:
    _current.reset(token)


def current_attempt_id() -> str | None:
    attempt = _current.get()
    return attempt.attempt_id if attempt else None


def link_decision(decision_id: str) -> None:
    """Attach the terminal decision to this request's accumulated attempt events."""
    attempt = _current.get()
    if attempt is None:
        return
    # The caller's DB transaction may still roll back. Defer linking until the
    # workflow stage has committed and its saved result can be verified.
    attempt.pending_decision_id = decision_id


def record(stage: str, status: str, *, duration_ms: float | None = None,
           provider: str | None = None, model: str | None = None,
           error_type: str | None = None, http_status: int | None = None,
           decision_id: str | None = None,
           validation_errors: dict[str, list[dict[str, str]]] | None = None,
           parser_trace: dict | None = None) -> None:
    attempt = _current.get()
    if attempt is None:
        return
    attempt.events.append(dict(stage=stage, status=status, duration_ms=duration_ms,
                               provider=provider, model=model, error_type=error_type,
                               http_status=http_status, decision_id=decision_id or attempt.decision_id,
                               validation_errors=validation_errors,
                               parser_trace=parser_trace))


def record_attempt(stage: str, status: str, **details: Any) -> None:
    """Save a completed model attempt before another provider call can start.

    Workflow boundaries bind the engine; standalone callers without a bound
    persistence context keep their existing explicit persistence behavior.
    """
    record(stage, status, **details)
    attempt = _current.get()
    if attempt is not None and attempt.persistence_engine is not None:
        from src.db.transaction_guard import assert_no_transaction
        assert_no_transaction()
        persist_incremental(attempt, attempt.persistence_engine)


def _http_status(exc: Exception) -> int | None:
    from src.services.llm_routing import http_status
    return http_status(exc)


@contextmanager
def span(stage: str, *, provider: str | None = None, model: str | None = None):
    started = monotonic()
    outcome: dict[str, Any] = {}
    try:
        yield outcome
    except Exception as exc:
        record(stage, "needs_clarification" if getattr(exc, "requires_clarification", False) else "failed",
               duration_ms=(monotonic() - started) * 1000,
               provider=provider, model=model, error_type=type(exc).__name__,
               http_status=_http_status(exc), validation_errors=getattr(exc, "validation_errors", None))
        raise
    else:
        record(stage, outcome.get("status", "applied"),
               duration_ms=(monotonic() - started) * 1000,
               provider=outcome.get("provider", provider), model=outcome.get("model", model),
               error_type=outcome.get("error_type"), http_status=outcome.get("http_status"),
               decision_id=outcome.get("decision_id"),
               validation_errors=outcome.get("validation_errors"))


def persist(attempt: Attempt, engine: Any) -> None:
    """Persist after the evaluation transaction finishes, even if it rolled back."""
    try:
        if any(event["stage"] == "request" and event["status"] == "failed"
               for event in attempt.events):
            logger.error("request_attempt_failed attempt_id=%s route=%s",
                         attempt.attempt_id, attempt.route)
        persist_incremental(attempt, engine)
    except Exception as exc:
        # Monitoring cannot grant or invalidate an action. Warning-level
        # structured lines remain available if the table or DB is unavailable.
        logger.warning("request_attempt_persist_failed attempt_id=%s error_type=%s",
                       attempt.attempt_id, type(exc).__name__)
        for sequence, event in enumerate(attempt.events, 1):
            logger.warning("request_attempt_event_fallback %s", json.dumps({
                "attempt_id": attempt.attempt_id, "sequence": sequence,
                "route": attempt.route, **event}, separators=(",", ":")))


def persist_incremental(attempt: Attempt, engine: Any) -> None:
    """Durably upsert events after each model attempt or short workflow stage."""
    attempt.persistence_engine = engine
    if not attempt.events:
        return
    update_links = attempt.persisted_link_version < attempt.decision_link_version
    selected = [(i, event) for i, event in enumerate(attempt.events, 1)
                if i > attempt.persisted_count or update_links]
    if not selected:
        return
    def with_correlation(event):
        payload = dict(event)
        if attempt.correlation_id:
            trace = dict(payload.get('parser_trace') or {})
            trace.setdefault('correlation_id', attempt.correlation_id)
            payload['parser_trace'] = trace
        return payload
    rows = [RequestAttemptEvent(attempt_id=attempt.attempt_id, sequence=i,
                                route=attempt.route, operation_id=attempt.operation_id,
                                **with_correlation(event))
            for i, event in selected]
    try:
        for row in rows:
            logger.info("request_attempt_event %s", json.dumps(row.model_dump(mode="json"),
                                                                separators=(",", ":")))
        # This service explicitly targets PostgreSQL; avoid an SQLite code path.
        from sqlalchemy.dialects.postgresql import insert
        from sqlmodel import select
        with Session(engine) as session:
            for row in rows:
                values = row.model_dump(exclude={'event_id'})
                existing = session.exec(select(RequestAttemptEvent).where(
                    RequestAttemptEvent.attempt_id == row.attempt_id,
                    RequestAttemptEvent.sequence == row.sequence)).first()
                if (existing is not None and existing.decision_id is not None
                        and values['decision_id'] not in {None, existing.decision_id}):
                    logger.error('monitoring_decision_link_conflict attempt_id=%s sequence=%s',
                                 row.attempt_id, row.sequence)
                    values['decision_id'] = existing.decision_id
                statement = insert(RequestAttemptEvent).values(**values).on_conflict_do_update(
                    constraint='uq_request_attempt_sequence',
                    set_={'operation_id': values['operation_id'],
                          'decision_id': values['decision_id']})
                session.exec(statement)
            session.commit()
        attempt.persisted_count = len(attempt.events)
        attempt.persisted_link_version = attempt.decision_link_version
    except Exception as exc:
        logger.warning("request_attempt_incremental_persist_failed attempt_id=%s error_type=%s",
                       attempt.attempt_id, type(exc).__name__)
        # Keep a structured fallback line while never including connection data.
        for sequence, event in enumerate(attempt.events, 1):
            logger.warning("request_attempt_event_fallback %s", json.dumps({
                "attempt_id": attempt.attempt_id, "sequence": sequence,
                "route": attempt.route, "operation_id": attempt.operation_id,
                **event}, separators=(",", ":")))


def flush_current(engine: Any) -> None:
    attempt = _current.get()
    if attempt is None:
        return
    persist_incremental(attempt, engine)


def commit_pending_link(engine: Any, operation_id: str) -> None:
    attempt = _current.get()
    if attempt is None or not attempt.pending_decision_id:
        return
    decision_id = attempt.pending_decision_id
    try:
        from src.models.decision import DecisionRecord
        from src.models.workflow import WorkflowOperation
        from src.services.workflow import _unpack
        with Session(engine) as session:
            if session.get(DecisionRecord, decision_id) is None:
                return
            operation = session.get(WorkflowOperation, operation_id)
            packed = (operation.checkpoint or {}).get('result') if operation else None
            if not packed:
                return
            body = json.loads(_unpack(packed).body)
            def contains(value):
                if isinstance(value, dict):
                    return any((key == 'decision_id' and item == decision_id) or contains(item)
                               for key, item in value.items())
                if isinstance(value, list):
                    return any(contains(item) for item in value)
                return False
            if not contains(body):
                return
        attempt.decision_id = decision_id
        for event in attempt.events:
            if event.get('decision_id') is None:
                event['decision_id'] = decision_id
        attempt.pending_decision_id = None
        attempt.decision_link_version += 1
        persist_incremental(attempt, engine)
    except Exception as exc:
        logger.warning('monitoring_decision_link_deferred operation_id=%s error_type=%s',
                       operation_id, type(exc).__name__)


def system_event(engine: Any, route: str, stage: str, status: str) -> None:
    """Persist a low-volume process heartbeat without attaching it to an operation."""
    attempt = Attempt(route=route)
    token = _current.set(attempt)
    try:
        record(stage, status)
    finally:
        _current.reset(token)
    persist_incremental(attempt, engine)
