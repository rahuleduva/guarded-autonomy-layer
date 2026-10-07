"""Pure Pydantic API contracts — no SQLModel tables (C.2).

The shared enums are imported here and re-exported for existing callers (C.1).
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


from src.models.enums import AutonomyLevel, DecisionOutcome, EscalationStatus, RiskLevel


class ActionRequest(BaseModel):
    """Structured action submitted to the gatekeeper, or parsed from NL."""

    request_id: str = Field(
        default_factory=lambda: str(uuid4()),
        description="Full UUIDv4, never a truncated id (A7)",
    )
    agent_id: str = Field(..., description="Acting agent")
    actor_role: str = Field(..., description="Must be a key of role_permissions")
    action_class: str = Field(..., description="Must be a key of risk_map")
    target_resource: str = Field(..., description="File/dir path; subject to the sandbox check (A1)")
    parameters: Dict[str, Any] = Field(default_factory=dict, description="Execution parameters")
    environment: str = Field(default="production", description="Environment name; participates in the decision (B7)")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class NaturalLanguageRequest(BaseModel):
    """Raw prompt entry point; parsed into an ActionRequest by llm_parser."""

    agent_id: str
    actor_role: str
    raw_prompt: str


class EvaluationResponse(BaseModel):
    """The single response shape both entry points return (A5/A8)."""

    status: str
    decision_id: Optional[str] = None
    request_id: str
    action_request: ActionRequest
    decision: DecisionOutcome
    risk_level: RiskLevel
    blast_radius: Dict[str, Any] = Field(default_factory=dict)
    policy_hash: str
    reason: str
    capability_token: Optional[str] = None
    autonomy_level: AutonomyLevel


class EscalationPackage(BaseModel):
    """What a human reviewer sees; persisted as package_json (C.3)."""

    escalation_id: str = Field(default_factory=lambda: str(uuid4()))
    decision_id: str
    request: ActionRequest
    risk_level: RiskLevel
    blast_radius: Dict[str, Any] = Field(default_factory=dict)
    semantic_warning: Optional[str] = None
    recommended_decision: str
    status: EscalationStatus = EscalationStatus.PENDING


class ApprovalDecision(BaseModel):
    """Reviewer payload for /api/v1/control/approve/{decision_id}."""

    decision_id: str
    reviewer_id: str
    approve: bool
    note: Optional[str] = None


class EvaluationResult(BaseModel):
    """Internal service result returned by policy_engine.evaluate — not the API response.

    The engine stays pure: it decides the TTL and never mints a token (C.6).
    """

    outcome: DecisionOutcome
    risk_level: RiskLevel
    blast_radius: Dict[str, Any] = Field(default_factory=dict)
    reason: str
    policy_hash: str
    ttl_seconds: Optional[int] = Field(
        default=None,
        description="None means no token is issuable (e.g. CRITICAL); the orchestrator skips minting",
    )
    autonomy_level: AutonomyLevel
    evidence: Dict[str, Any] = Field(default_factory=dict)


class ReplayResult(BaseModel):
    decision_id: str
    original_outcome: DecisionOutcome
    replayed_outcome: DecisionOutcome
    is_consistent: bool
    policy_hash: str
    discrepancy_reason: Optional[str] = None


class ExecutionResult(BaseModel):
    execution_id: str
    decision_id: str
    status: str = Field(..., description="EXECUTED or REJECTED")
    before_state: Dict[str, Any] = Field(default_factory=dict)
    after_state: Dict[str, Any] = Field(default_factory=dict)


class RollbackResult(BaseModel):
    execution_id: str
    status: str = Field(..., description="COMPENSATED or FAILED")
    restored_state: Dict[str, Any] = Field(default_factory=dict)
