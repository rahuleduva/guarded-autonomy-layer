"""Operational events and parser traces, independent of the decision ledger."""
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from sqlalchemy import Column, UniqueConstraint, Index
from sqlmodel import Field, SQLModel
from src.models.types import JSONType


class RequestAttemptEvent(SQLModel, table=True):
    __tablename__ = "request_attempt_events"

    event_id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    operation_id: str | None = Field(default=None, foreign_key='workflow_operations.operation_id', index=True)
    attempt_id: str = Field(index=True)
    sequence: int
    route: str
    stage: str
    status: str
    duration_ms: Optional[float] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    error_type: Optional[str] = None
    http_status: Optional[int] = None
    validation_errors: Optional[dict] = Field(
        default=None, sa_column=Column(JSONType, nullable=True),
    )
    parser_trace: Optional[dict] = Field(
        default=None, sa_column=Column(JSONType, nullable=True),
    )
    decision_id: Optional[str] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint('attempt_id', 'sequence', name='uq_request_attempt_sequence'),
        Index('ix_request_attempt_operation_sequence', 'operation_id', 'sequence'),
    )
