"""Persistent control audit and simulated execution state."""

from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from sqlalchemy import Column, ForeignKey, String
from sqlmodel import Field, SQLModel

from src.models.types import JSONType


class PolicyPromotionEvent(SQLModel, table=True):
    __tablename__ = "policy_promotion_events"
    event_id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    policy_hash: str = Field(sa_column=Column(String, ForeignKey("policies.policy_hash", ondelete="RESTRICT"), nullable=False))
    reviewer_id: str
    approved: bool
    promoted: bool
    evidence_json: dict = Field(sa_type=JSONType)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SimulatedResource(SQLModel, table=True):
    __tablename__ = "simulated_resources"
    resource: str = Field(primary_key=True)
    value_json: dict = Field(sa_type=JSONType)


class ExecutionRecord(SQLModel, table=True):
    __tablename__ = "execution_records"
    execution_id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    decision_id: str = Field(sa_column=Column(String, ForeignKey("decision_records.decision_id", ondelete="RESTRICT"), nullable=False, unique=True))
    status: str = Field(default="EXECUTED")
    before_state: dict = Field(sa_type=JSONType)
    after_state: dict = Field(sa_type=JSONType)
    compensation_decision_id: Optional[str] = Field(default=None, foreign_key="decision_records.decision_id")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
