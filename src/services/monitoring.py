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
    attempt.decision_id = decision_id
    for event in attempt.events:
        if event.get("decision_id") is None:
            event["decision_id"] = decision_id


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
        rows = [RequestAttemptEvent(attempt_id=attempt.attempt_id, sequence=i,
                                    route=attempt.route, **event)
                for i, event in enumerate(attempt.events, 1)]
        for row in rows:
            logger.info("request_attempt_event %s", json.dumps(row.model_dump(mode="json"),
                                                                separators=(",", ":")))
        with Session(engine) as session:
            session.add_all(rows)
            session.commit()
    except Exception as exc:
        # Monitoring cannot grant or invalidate an action. Warning-level
        # structured lines remain available if the table or DB is unavailable.
        logger.warning("request_attempt_persist_failed attempt_id=%s error_type=%s",
                       attempt.attempt_id, type(exc).__name__)
        for sequence, event in enumerate(attempt.events, 1):
            logger.warning("request_attempt_event_fallback %s", json.dumps({
                "attempt_id": attempt.attempt_id, "sequence": sequence,
                "route": attempt.route, **event}, separators=(",", ":")))
