# Escalation queue SQLModel table
import datetime
import uuid
from typing import Any, Dict, Optional
from sqlmodel import Field, SQLModel, Column
from sqlalchemy import ForeignKey, Index, String
from src.models.constraints import enum_check
from src.models.enums import EscalationStatus
from src.models.types import JSONType


class EscalationQueueItem(SQLModel, table=True):
    __tablename__ = "escalation_queue"

    escalation_id: str = Field(primary_key=True, default_factory=lambda: str(uuid.uuid4()))
    decision_id: str = Field(
        sa_column=Column(
            String,
            ForeignKey("decision_records.decision_id", ondelete="RESTRICT"),
            index=True,
            nullable=False
        )
    )
    package_json: dict = Field(..., sa_type=JSONType)
    status: str = Field(
        sa_column=Column(
            String,
            enum_check("status", EscalationStatus, "ck_escalation_status"),
            default=EscalationStatus.PENDING.value,
            index=True,
            nullable=False
        )
    )
    reviewer_id: Optional[str] = None
    decided_at: Optional[datetime.datetime] = None
    created_at: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc))

    __table_args__ = ()
