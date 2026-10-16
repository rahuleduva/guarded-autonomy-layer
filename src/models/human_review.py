"""Typed explanation and audit contracts for combined human review."""
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import CheckConstraint, Column, ForeignKey, String, UniqueConstraint
from sqlmodel import Field as SQLField, SQLModel

from src.models.constraints import enum_check
from src.models.enums import HumanReviewDecision


class StrictReviewContract(BaseModel):
    model_config = ConfigDict(extra="forbid", revalidate_instances="always")


class ReviewRequirement(StrictReviewContract):
    requirement_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    description: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    facts: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


class RequirementExplanation(StrictReviewContract):
    requirement_id: str = Field(min_length=1)
    explanation: str = Field(min_length=1)
    possible_consequences: list[str] = Field(default_factory=list)
    reviewer_questions: list[str] = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)


class ReviewExplanation(StrictReviewContract):
    version: Literal["human-review-explanation-v1"] = "human-review-explanation-v1"
    action_summary: str = Field(min_length=1)
    explanations: list[RequirementExplanation] = Field(min_length=1)
    provenance: dict[str, Any] = Field(default_factory=dict)


class ReviewExplanationOutput(StrictReviewContract):
    action_summary: str = Field(min_length=1)
    explanations: list[RequirementExplanation] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_requirements(self):
        ids = [item.requirement_id for item in self.explanations]
        if len(ids) != len(set(ids)):
            raise ValueError("explanation repeats a requirement_id")
        return self


class HumanReviewResponse(StrictReviewContract):
    status: Literal["APPROVED", "REJECTED"]
    review_id: str
    original_decision_id: str
    authorization_status: Literal["NOT_REQUESTED", "ALLOWED", "BLOCKED"]
    authorization_decision_id: str | None = None
    authorization_reason: str
    evaluation: dict[str, Any] | None = None
    capability_token: str | None = None
    new_policy_version: None = None


class HumanReviewWrite(StrictReviewContract):
    review_id: str = Field(default_factory=lambda: str(uuid4()))
    escalation_id: str
    original_decision_id: str
    package_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    action_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer_id: str
    decision: HumanReviewDecision
    note: str | None = None
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    authorization_decision_id: str | None = None

    @model_validator(mode="after")
    def validate_review(self):
        if not self.reviewer_id.strip():
            raise ValueError("reviewer_id must not be blank")
        if self.note is not None and not self.note.strip():
            raise ValueError("review note must not be blank when supplied")
        if self.decision == "APPROVE" and (self.note is None or not self.note.strip()):
            raise ValueError("approval requires a nonblank note")
        if self.decision == "APPROVE" and not self.authorization_decision_id:
            raise ValueError("approval requires a linked authorization decision")
        if self.decision == "REJECT" and self.authorization_decision_id is not None:
            raise ValueError("rejection cannot link an authorization decision")
        return self


class ReviewApprovalEvidence(StrictReviewContract):
    approved: Literal[True]
    reviewer_id: str = Field(min_length=1)
    original_decision_id: str = Field(min_length=1)
    action_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_id: str = Field(min_length=1)
    package_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class HumanReviewFinalizationEvidence(StrictReviewContract):
    version: Literal["human_review_finalization_v1"]
    review_id: str = Field(min_length=1)
    escalation_id: str = Field(min_length=1)
    original_decision_id: str = Field(min_length=1)
    package_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    action_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer_id: str = Field(min_length=1)
    review_note: str = Field(min_length=1)
    original_autonomy_snapshot: dict[str, Any] | None = None
    active_policy_hash: str = Field(min_length=1)


class HumanReviewRecord(SQLModel, table=True):
    """One accepted reviewer submission; policy decisions stay in decision_records."""

    __tablename__ = "human_review_records"

    review_id: str = SQLField(primary_key=True, default_factory=lambda: str(uuid4()))
    escalation_id: str = SQLField(sa_column=Column(
        String, ForeignKey("escalation_queue.escalation_id", ondelete="RESTRICT"),
        nullable=False, index=True,
    ))
    original_decision_id: str = SQLField(sa_column=Column(
        String, ForeignKey("decision_records.decision_id", ondelete="RESTRICT"), nullable=False,
    ))
    package_fingerprint: str
    action_fingerprint: str
    reviewer_id: str
    decision: str = SQLField(sa_column=Column(
        String, enum_check("decision", HumanReviewDecision, "ck_human_review_decision"), nullable=False,
    ))
    note: str | None = None
    submitted_at: datetime = SQLField(default_factory=lambda: datetime.now(timezone.utc))
    authorization_decision_id: str | None = SQLField(default=None, sa_column=Column(
        String, ForeignKey("decision_records.decision_id", ondelete="RESTRICT"), nullable=True,
    ))

    __table_args__ = (
        UniqueConstraint("escalation_id", name="uq_human_review_escalation"),
        CheckConstraint("length(trim(reviewer_id)) > 0", name="ck_human_review_reviewer_nonblank"),
        CheckConstraint("length(package_fingerprint) = 64 AND length(action_fingerprint) = 64",
                        name="ck_human_review_fingerprint_lengths"),
        CheckConstraint("note IS NULL OR length(trim(note)) > 0",
                        name="ck_human_review_note_nonblank_when_present"),
        CheckConstraint(
            "decision != 'APPROVE' OR (note IS NOT NULL AND length(trim(note)) > 0)",
            name="ck_human_review_approval_note",
        ),
        CheckConstraint(
            "(decision = 'APPROVE' AND authorization_decision_id IS NOT NULL) OR "
            "(decision = 'REJECT' AND authorization_decision_id IS NULL)",
            name="ck_human_review_authorization_link",
        ),
    )

    @classmethod
    def from_contract(cls, contract: HumanReviewWrite) -> "HumanReviewRecord":
        data = HumanReviewWrite.model_validate(contract).model_dump()
        data["decision"] = data["decision"].value
        return cls(**data)
