# Decision ledger SQLModel table (with reason/evidence)
import datetime
from typing import Any, Dict, Optional

from sqlmodel import Field, SQLModel, Column
from sqlalchemy import CheckConstraint, ForeignKey, String
from src.models.constraints import enum_check
from src.models.enums import AutonomyLevel, DecisionOutcome, DecisionStage, RiskLevel
from src.models.types import JSONType


class DecisionRecord(SQLModel, table=True):
    __tablename__ = "decision_records"

    decision_id: str = Field(primary_key=True)
    decision_stage: str = Field(default=DecisionStage.POLICY.value, sa_column=Column(
        String, enum_check("decision_stage", DecisionStage, "ck_decision_stage"),
        nullable=False, server_default=DecisionStage.POLICY.value, index=True,
    ))
    request_id: str = Field(unique=True, index=True)
    agent_id: str = Field(index=True)
    actor_role: str
    action_class: Optional[str] = None
    target_resource: Optional[str] = None
    request_json: Dict[str, Any] = Field(..., sa_type=JSONType)
    risk_level: Optional[str] = Field(default=None, sa_column=Column(
        String,
        enum_check("risk_level", RiskLevel, "ck_decision_risk_level"),
        nullable=True
    ))
    autonomy_level: Optional[str] = Field(default=None, sa_column=Column(
        String,
        enum_check("autonomy_level", AutonomyLevel, "ck_decision_autonomy_level"),
        nullable=True
    ))
    outcome: str = Field(sa_column=Column(
        String,
        enum_check("outcome", DecisionOutcome, "ck_decision_outcome"),
        nullable=False
    ))
    policy_hash: Optional[str] = Field(
        default=None,
        sa_column=Column(
            String,
            ForeignKey("policies.policy_hash", ondelete="RESTRICT"),
            index=True,
            nullable=True
        )
    )
    reason: str
    evidence_json: Dict[str, Any] = Field(..., sa_type=JSONType)
    capability_token_hash: Optional[str] = None
    reviewer_id: Optional[str] = None
    created_at: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc))

    __table_args__ = (
        CheckConstraint(
            "length(trim(decision_id)) > 0 AND length(trim(request_id)) > 0 "
            "AND length(trim(agent_id)) > 0 AND length(trim(actor_role)) > 0 "
            "AND length(trim(reason)) > 0",
            name="ck_decision_required_text",
        ),
        CheckConstraint(
            f"(decision_stage = '{DecisionStage.POLICY.value}' "
            "AND action_class IS NOT NULL AND length(trim(action_class)) > 0 "
            "AND target_resource IS NOT NULL AND length(trim(target_resource)) > 0 "
            "AND risk_level IS NOT NULL AND autonomy_level IS NOT NULL "
            "AND policy_hash IS NOT NULL AND length(trim(policy_hash)) > 0) OR "
            f"(decision_stage = '{DecisionStage.PARSER.value}' "
            f"AND outcome = '{DecisionOutcome.DENIED.value}' "
            "AND action_class IS NULL AND target_resource IS NULL "
            "AND risk_level IS NULL AND autonomy_level IS NULL AND policy_hash IS NULL "
            "AND capability_token_hash IS NULL AND reviewer_id IS NULL)",
            name="ck_decision_stage_fields",
        ),
        CheckConstraint(
            f"capability_token_hash IS NULL OR (decision_stage = '{DecisionStage.POLICY.value}' "
            f"AND outcome = '{DecisionOutcome.ALLOWED.value}')",
            name="ck_decision_capability_stage",
        ),
    )
