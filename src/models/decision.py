# Decision ledger SQLModel table (with reason/evidence)
import datetime
from typing import Any, Dict, Optional

from sqlmodel import Field, SQLModel, Column
from sqlalchemy import ForeignKey, Index, String
from src.models.constraints import enum_check
from src.models.enums import AutonomyLevel, DecisionOutcome, RiskLevel
from src.models.types import JSONType


class DecisionRecord(SQLModel, table=True):
    __tablename__ = "decision_records"

    decision_id: str = Field(primary_key=True)
    request_id: str = Field(unique=True, index=True)
    agent_id: str = Field(index=True)
    actor_role: str
    action_class: str
    target_resource: str
    request_json: Dict[str, Any] = Field(..., sa_type=JSONType)
    risk_level: str = Field(sa_column=Column(
        String,
        enum_check("risk_level", RiskLevel, "ck_decision_risk_level"),
        nullable=False
    ))
    autonomy_level: str = Field(sa_column=Column(
        String,
        enum_check("autonomy_level", AutonomyLevel, "ck_decision_autonomy_level"),
        nullable=False
    ))
    outcome: str = Field(sa_column=Column(
        String,
        enum_check("outcome", DecisionOutcome, "ck_decision_outcome"),
        nullable=False
    ))
    policy_hash: str = Field(
        sa_column=Column(
            String,
            ForeignKey("policies.policy_hash", ondelete="RESTRICT"),
            index=True,
            nullable=False
        )
    )
    reason: str
    evidence_json: Dict[str, Any] = Field(..., sa_type=JSONType)
    capability_token_hash: Optional[str] = None
    reviewer_id: Optional[str] = None
    created_at: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc))

    __table_args__ = ()
