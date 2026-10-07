# Policy-shadow records SQLModel table
import datetime
import uuid

from sqlmodel import Field, SQLModel, Column
from sqlalchemy import ForeignKey, Index, String
from src.models.constraints import enum_check
from src.models.enums import DecisionOutcome


class ShadowRecord(SQLModel, table=True):
    __tablename__ = "shadow_records"

    shadow_id: str = Field(primary_key=True, default_factory=lambda: str(uuid.uuid4()))
    decision_id: str = Field(
        sa_column=Column(
            String,
            ForeignKey("decision_records.decision_id", ondelete="RESTRICT"),
            index=True,
            nullable=False
        )
    )
    candidate_policy_hash: str = Field(
        sa_column=Column(
            String,
            ForeignKey("policies.policy_hash", ondelete="RESTRICT"),
            index=True,
            nullable=False
        )
    )
    would_have_decided: str = Field(sa_column=Column(
        String,
        enum_check("would_have_decided", DecisionOutcome, "ck_shadow_would_have_decided"),
        nullable=False
    ))
    created_at: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc))

    # Already declared inline
    # __table_args__ = (
    #     Index("ix_shadow_records_decision_id", "decision_id"),
    #     Index("ix_shadow_records_candidate_policy_hash", "candidate_policy_hash"),
    # )
