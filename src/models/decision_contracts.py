"""Validated, stage-specific contracts for every decision-ledger write.

SQLModel table constructors are not the validation boundary. Writers explicitly
validate these contracts before constructing or updating a DecisionRecord.
"""
from datetime import datetime, timezone
from typing import Annotated, Any, Literal
from uuid import uuid4
from pydantic import Field, TypeAdapter, model_validator
from src.models.enums import AutonomyLevel, DecisionOutcome, DecisionStage, RiskLevel
from src.models.human_review import HumanReviewFinalizationEvidence, ReviewApprovalEvidence
from src.models.request import ActionRequest, NaturalLanguageRequest, ValidatedContract, ValidationErrors


class ParserDecisionEvidence(ValidatedContract):
    interpretation_id: str
    correlation_id: str | None = None
    error_type: str
    error_message: str
    validation_errors: ValidationErrors = Field(min_length=1)
    analysis: dict[str, Any] | None = None
    parser_attempts: int | None = Field(default=None, ge=1, strict=True)
    correction_message: str | None = None

class DecisionWriteBase(ValidatedContract):
    decision_id: str = Field(default_factory=lambda: str(uuid4()))
    request_id: str
    agent_id: str
    actor_role: str
    reason: str
    request_json: NaturalLanguageRequest | ActionRequest
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def request_identity_matches(self):
        for field in ("request_id", "agent_id", "actor_role"):
            if getattr(self, field) != getattr(self.request_json, field):
                raise ValueError(f"decision {field} differs from its recorded request")
        return self

    def table_values(self) -> dict[str, Any]:
        # JSON columns need JSON-compatible nested values; timestamps need datetime.
        values = self.model_dump(mode="json")
        values["created_at"] = self.created_at
        return values


class ParserDecisionWrite(DecisionWriteBase):
    decision_stage: Literal[DecisionStage.PARSER] = DecisionStage.PARSER
    outcome: Literal[DecisionOutcome.DENIED] = DecisionOutcome.DENIED
    request_json: NaturalLanguageRequest
    evidence_json: ParserDecisionEvidence
    action_class: None = None
    target_resource: None = None
    risk_level: None = None
    autonomy_level: None = None
    policy_hash: None = None
    capability_token_hash: None = None
    reviewer_id: None = None


class PolicyDecisionWrite(DecisionWriteBase):
    decision_stage: Literal[DecisionStage.POLICY] = DecisionStage.POLICY
    outcome: DecisionOutcome
    request_json: ActionRequest
    evidence_json: dict[str, Any]
    action_class: str
    target_resource: str
    risk_level: RiskLevel
    autonomy_level: AutonomyLevel
    policy_hash: str
    capability_token_hash: str | None = None
    reviewer_id: str | None = None

    @model_validator(mode="after")
    def validate_policy_fields(self):
        from src.services.semantic_pipeline import verify_frozen
        verify_frozen(self.evidence_json)
        unsupported = self.evidence_json.get('unsupported_action')
        if unsupported is not None:
            from src.models.semantic import UnsupportedActionEvidence
            refusal = UnsupportedActionEvidence.model_validate(unsupported)
            if refusal.action_class != self.action_class or self.outcome != DecisionOutcome.DENIED:
                raise ValueError('unsupported runtime action must record the matching DENIED decision')
        for field in ("action_class", "target_resource"):
            if getattr(self, field) != getattr(self.request_json, field):
                raise ValueError(f"decision {field} differs from its recorded action")
        if self.capability_token_hash is not None and self.outcome != DecisionOutcome.ALLOWED:
            raise ValueError("only an allowed policy decision may store a capability token hash")
        finalization_data = self.evidence_json.get("human_review_finalization")
        if finalization_data is not None:
            finalization = HumanReviewFinalizationEvidence.model_validate(finalization_data)
            approval = ReviewApprovalEvidence.model_validate(self.evidence_json.get("review_approval"))
            from src.services.action_binding import fingerprint
            if (finalization.action_fingerprint != fingerprint(self.request_json)
                    or approval.action_fingerprint != finalization.action_fingerprint
                    or approval.review_id != finalization.review_id
                    or approval.package_fingerprint != finalization.package_fingerprint
                    or approval.original_decision_id != finalization.original_decision_id
                    or approval.reviewer_id != finalization.reviewer_id
                    or self.reviewer_id != finalization.reviewer_id
                    or self.policy_hash != finalization.active_policy_hash):
                raise ValueError("human review finalization evidence does not match its decision")
        return self


DecisionWrite = Annotated[ParserDecisionWrite | PolicyDecisionWrite, Field(discriminator="decision_stage")]
DECISION_WRITE_ADAPTER = TypeAdapter(DecisionWrite)
