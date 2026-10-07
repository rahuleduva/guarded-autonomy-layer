# Autonomy state SQLModel table
from typing import Optional
import datetime
from uuid import uuid4
from sqlmodel import Field, SQLModel, Column
from sqlalchemy import UniqueConstraint, String, ForeignKey
from src.models.constraints import enum_check
from src.models.enums import AutonomyLevel
from src.models.types import JSONType


class AutonomyState(SQLModel, table=True):
    __tablename__ = "autonomy_state"

    id: Optional[int] = Field(default=None, primary_key=True)
    agent_id: str = Field(index=True)
    action_class: str = Field(index=True)
    mode: str = Field(
        sa_column=Column(
            String,
            enum_check("mode", AutonomyLevel, "ck_autonomy_mode"),
            default=AutonomyLevel.SHADOW.value,
            nullable=False
        )
    )
    streak: int = Field(default=0)
    reviewer_id: Optional[str] = None
    promoted_at: Optional[datetime.datetime] = None
    updated_at: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc))

    __table_args__ = (UniqueConstraint("agent_id", "action_class"),)


class AutonomyPromotionEvent(SQLModel, table=True):
    """Append-only evidence and attribution for promotion decisions."""

    __tablename__ = "autonomy_promotion_events"

    event_id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    autonomy_state_id: int = Field(sa_column=Column(
        "autonomy_state_id", ForeignKey("autonomy_state.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    ))
    previous_mode: str = Field(sa_column=Column(
        String, enum_check("previous_mode", AutonomyLevel, "ck_promotion_previous_mode"),
        nullable=False,
    ))
    requested_mode: str = Field(sa_column=Column(
        String, enum_check("requested_mode", AutonomyLevel, "ck_promotion_requested_mode"),
        nullable=False,
    ))
    reviewer_id: Optional[str] = None
    approved: bool
    promoted: bool
    reason: str
    evidence_json: dict = Field(sa_type=JSONType)
    created_at: datetime.datetime = Field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
