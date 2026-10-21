"""Pure Pydantic API contracts — no SQLModel tables (C.2).

The shared enums are imported here and re-exported for existing callers (C.1).
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator


from src.models.semantic import SemanticSummary
from src.models.enums import AutonomyLevel, DecisionOutcome, DecisionStage, EscalationStatus, RiskLevel
from src.models.human_review import ReviewExplanation, ReviewRequirement


class ValidatedContract(BaseModel):
    """Revalidate internal model instances as well as incoming dictionaries."""

    model_config = ConfigDict(extra="forbid", revalidate_instances="always")

    @field_validator("*", mode="after")
    @classmethod
    def nonblank_strings(cls, value, info):
        if info.field_name not in {"note", "review_note"} and isinstance(value, str) and not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("validation_errors", check_fields=False)
    @classmethod
    def validate_error_fields(cls, value):
        if value is not None and any(not field.strip() or not issues for field, issues in value.items()):
            raise ValueError("each validation error field requires at least one issue")
        return value


class ValidationIssue(ValidatedContract):
    code: str
    message: str


ValidationErrors = Dict[str, list[ValidationIssue]]


class ActionRequest(ValidatedContract):
    """Structured action submitted to the gatekeeper, or parsed from NL."""

    _semantic_analysis: Optional[Dict[str, Any]] = PrivateAttr(default=None)

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


class NaturalLanguageRequest(ValidatedContract):
    """Raw prompt entry point; parsed into an ActionRequest by llm_parser."""

    request_id: str = Field(default_factory=lambda: str(uuid4()))
    agent_id: str
    actor_role: str
    raw_prompt: str = Field(min_length=1, max_length=8192)


class UnsupportedActionRefusal(ValidatedContract):
    """Machine-readable reason the runtime cannot handle a submitted action."""

    code: Literal['UNSUPPORTED_ACTION']
    action_class: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class EvaluationResponse(ValidatedContract):
    """The single response shape both entry points return (A5/A8)."""

    status: Literal["ok"] = "ok"
    decision_stage: Literal[DecisionStage.POLICY] = DecisionStage.POLICY
    interpretation_id: Optional[str] = None
    decision_id: str
    request_id: str
    action_request: ActionRequest
    decision: DecisionOutcome
    risk_level: RiskLevel
    blast_radius: Dict[str, Any] = Field(default_factory=dict)
    policy_hash: str
    reason: str
    capability_token: Optional[str] = None
    autonomy_level: AutonomyLevel
    semantic_summary: Optional[SemanticSummary] = None
    semantic_mode: Optional[Literal['observe', 'review']] = None
    unsupported_action: Optional[UnsupportedActionRefusal] = None

    @model_validator(mode="after")
    def validate_policy_response(self):
        if (self.semantic_summary is None) != (self.semantic_mode is None):
            raise ValueError('semantic summary and mode must be supplied together')
        if self.request_id != self.action_request.request_id:
            raise ValueError("response request_id differs from its action request")
        if self.capability_token is not None and self.decision != DecisionOutcome.ALLOWED:
            raise ValueError("only an allowed policy decision may return a capability token")
        if self.unsupported_action is not None:
            if (self.decision != DecisionOutcome.DENIED
                    or self.unsupported_action.action_class != self.action_request.action_class
                    or self.unsupported_action.reason != self.reason):
                raise ValueError('unsupported-action refusal must match the denied action and reason')
        return self


class AgentReviewStatus(ValidatedContract):
    """Agent-visible review progress, addressed by the original decision ID."""

    original_decision_id: str
    status: Literal['PREPARING', 'AWAITING_REVIEW', 'REJECTED', 'APPROVED_BLOCKED', 'APPROVED_ALLOWED']
    reason: str
    authorization_decision_id: str | None = None
    evaluation: EvaluationResponse | None = None

    @model_validator(mode='after')
    def consistent_review_status(self):
        approved = self.status in {'APPROVED_ALLOWED', 'APPROVED_BLOCKED'}
        if approved:
            if self.evaluation is None or self.authorization_decision_id != self.evaluation.decision_id:
                raise ValueError('approved review requires its linked authorization evaluation')
            if (self.evaluation.decision == DecisionOutcome.ALLOWED) != (self.status == 'APPROVED_ALLOWED'):
                raise ValueError('review status differs from authorization outcome')
            if self.status == 'APPROVED_ALLOWED' and not self.evaluation.capability_token:
                raise ValueError('allowed review requires its original capability')
        elif self.authorization_decision_id is not None or self.evaluation is not None:
            raise ValueError('unapproved review cannot return authorization')
        return self


class EscalationPackage(ValidatedContract):
    """What a human reviewer sees; persisted as package_json (C.3)."""

    escalation_id: str = Field(default_factory=lambda: str(uuid4()))
    decision_id: str
    decision_stage: Literal[DecisionStage.POLICY] = DecisionStage.POLICY
    request: ActionRequest
    risk_level: RiskLevel
    blast_radius: Dict[str, Any] = Field(default_factory=dict)
    semantic_warning: Optional[str] = None
    semantic_findings: Dict[str, Any] = Field(default_factory=dict)
    recommended_decision: str
    package_version: Optional[str] = None
    original_outcome: Optional[DecisionOutcome] = None
    original_policy_hash: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    action_fingerprint: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    review_requirements: list[ReviewRequirement] = Field(default_factory=list)
    explanation: Optional[ReviewExplanation] = None
    package_fingerprint: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    status: EscalationStatus = EscalationStatus.PENDING
    reviewer_id: Optional[str] = None
    review_note: Optional[str] = None
    decided_at: Optional[datetime] = None

    @model_validator(mode="after")
    def validate_review_state(self):
        if self.semantic_findings:
            from src.services.semantic_pipeline import validate_pipeline
            validate_pipeline(self.semantic_findings)
        if self.status == EscalationStatus.PREPARING and (
                self.package_fingerprint is not None or self.explanation is not None or self.review_requirements):
            raise ValueError('a preparing escalation cannot claim a completed explanation')
        if self.status in {EscalationStatus.PENDING, EscalationStatus.PREPARING}:
            if self.reviewer_id is not None or self.decided_at is not None or self.review_note is not None:
                raise ValueError("a pending escalation cannot claim a reviewer decision")
        elif self.reviewer_id is None or self.decided_at is None:
            raise ValueError("a reviewed escalation requires reviewer_id and decided_at")
        if self.package_fingerprint is not None:
            if (not self.package_version or not self.original_outcome or not self.original_policy_hash
                    or not self.action_fingerprint or not self.review_requirements or not self.explanation):
                raise ValueError("a prepared review package requires complete frozen review context")
            required_ids = {item.requirement_id for item in self.review_requirements}
            explained_ids = [item.requirement_id for item in self.explanation.explanations]
            if len(explained_ids) != len(set(explained_ids)) or set(explained_ids) != required_ids:
                raise ValueError("review explanation must cover every requirement exactly once")
        return self


class ApprovalDecision(ValidatedContract):
    """Reviewer payload for /api/v1/control/approve/{decision_id}."""

    decision_id: str
    reviewer_id: str
    approve: bool = Field(strict=True)
    note: Optional[str] = None
    package_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def approval_note_nonblank(self):
        if self.note is not None and not self.note.strip():
            raise ValueError("review note must not be blank when supplied")
        if self.approve and (self.note is None or not self.note.strip()):
            raise ValueError("approval note must not be blank")
        return self


class EvaluationResult(ValidatedContract):
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
        strict=True,
        gt=0,
        description="None means no token is issuable (e.g. CRITICAL); the orchestrator skips minting",
    )
    autonomy_level: AutonomyLevel
    evidence: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_capability_ttl(self):
        if self.outcome == DecisionOutcome.ALLOWED:
            if self.ttl_seconds is None or self.ttl_seconds <= 0:
                raise ValueError("an allowed policy decision requires a positive token TTL")
        elif self.ttl_seconds is not None:
            raise ValueError("only an allowed policy decision may specify a token TTL")
        return self


class ReplayResult(ValidatedContract):
    decision_id: str
    decision_stage: Literal[DecisionStage.POLICY] = DecisionStage.POLICY
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
    status: Literal["COMPENSATED", "PENDING_APPROVAL", "FAILED"] = Field(
        ..., description="COMPENSATED, pending human approval, or failed"
    )
    compensation_decision_id: Optional[str] = None
    restored_state: Dict[str, Any] = Field(default_factory=dict)


class ClarificationAnswer(ValidatedContract):
    """A complete revised instruction, not an approval or a partial parameter patch."""

    agent_id: str
    actor_role: str
    revision: int = Field(ge=1, strict=True)
    clarified_prompt: str = Field(min_length=1, max_length=8192)


class ClarificationResponse(ValidatedContract):
    status: Literal["needs_clarification"] = "needs_clarification"
    request_id: str
    interpretation_id: str
    revision: int = Field(ge=1, strict=True)
    questions: list[str] = Field(min_length=1)
    validation_errors: ValidationErrors = Field(min_length=1)
    next_step: str = "Submit a complete clarified instruction to the clarification endpoint. Include all intended operations, restrictions, paths, content, and environment."

    @field_validator("questions")
    @classmethod
    def validate_questions(cls, value):
        if any(not question.strip() for question in value):
            raise ValueError("clarification questions must not be blank")
        return value


class ParserRejectionResponse(ValidatedContract):
    status: Literal["rejected"] = "rejected"
    decision_stage: Literal[DecisionStage.PARSER] = DecisionStage.PARSER
    decision: Literal[DecisionOutcome.DENIED] = DecisionOutcome.DENIED
    decision_id: str
    request_id: str
    interpretation_id: str
    revision: int = Field(ge=1, strict=True)
    detail: str
    validation_errors: ValidationErrors = Field(min_length=1)


class ParserFailureResponse(ValidatedContract):
    """Operational failures are not decision-ledger denials."""

    status: Literal["provider_failed"] = "provider_failed"
    request_id: str
    interpretation_id: str
    revision: int = Field(ge=1, strict=True)
    detail: str
    validation_errors: ValidationErrors = Field(min_length=1)
