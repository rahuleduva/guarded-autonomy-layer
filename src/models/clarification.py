"""Durable natural-language interpretation and clarification history."""
from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4
from typing import Any
from sqlalchemy import Column, String, ForeignKey
from sqlmodel import Field, SQLModel
from src.models.constraints import enum_check
from src.models.types import JSONType
from src.models.enums import DecisionStage
from src.models.request import ActionRequest, ValidatedContract, ValidationIssue
from pydantic import Field as ContractField, model_validator


class InterpretationStatus(str, Enum):
    NEEDS_CLARIFICATION = 'needs_clarification'
    EVALUATED = 'evaluated'
    REJECTED = 'rejected'
    PROVIDER_FAILED = 'provider_failed'


class InterpretationTurn(ValidatedContract):
    revision: int = ContractField(ge=1, strict=True)
    request_id: str
    prompt: str = ContractField(min_length=1, max_length=8192)
    submitted_at: datetime
    correlation_id: str | None = None
    status: InterpretationStatus
    questions: list[str] = ContractField(default_factory=list)
    validation_errors: dict[str, list[ValidationIssue]] | None = None
    error_type: str | None = None
    error_message: str | None = None
    analysis: dict[str, Any] | None = None
    parser_attempts: int | None = ContractField(default=None, ge=1, strict=True)
    correction_message: str | None = None
    decision_id: str | None = None
    decision_stage: DecisionStage | None = None
    action_request: ActionRequest | None = None

    @model_validator(mode="after")
    def validate_turn_state(self):
        if any(not question.strip() for question in self.questions):
            raise ValueError("clarification questions must not be blank")
        if self.status == InterpretationStatus.NEEDS_CLARIFICATION:
            if not self.questions or not self.validation_errors:
                raise ValueError("a pending clarification requires questions and error details")
        elif self.questions:
            raise ValueError("only a pending clarification may have open questions")
        if self.status == InterpretationStatus.EVALUATED:
            if (not self.decision_id or self.decision_stage != DecisionStage.POLICY
                    or self.action_request is None):
                raise ValueError("an evaluated turn requires its policy decision and action")
            if self.request_id != self.action_request.request_id:
                raise ValueError("turn request_id differs from its action")
        elif self.status == InterpretationStatus.REJECTED:
            if (not self.decision_id or self.decision_stage != DecisionStage.PARSER
                    or not self.validation_errors):
                raise ValueError("a rejected turn requires its parser decision and validation errors")
            if self.action_request is not None:
                raise ValueError("a parser rejection must not claim an executable action")
        elif self.decision_id is not None or self.decision_stage is not None or self.action_request is not None:
            raise ValueError("clarification and provider failures are not terminal decisions")
        if self.status == InterpretationStatus.PROVIDER_FAILED and not self.error_type:
            raise ValueError("a provider failure requires its error type")
        return self


class InterpretationWrite(ValidatedContract):
    interpretation_id: str
    agent_id: str
    actor_role: str
    original_prompt: str = ContractField(min_length=1, max_length=8192)
    status: InterpretationStatus
    revision: int = ContractField(ge=1, strict=True)
    turns: list[InterpretationTurn] = ContractField(min_length=1)
    questions: list[str]
    decision_id: str | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_history(self):
        if self.turns[0].prompt != self.original_prompt:
            raise ValueError("the original instruction must match the first saved turn")
        if [turn.revision for turn in self.turns] != list(range(1, self.revision + 1)):
            raise ValueError("clarification history must contain every revision exactly once")
        if any(turn.status not in {InterpretationStatus.NEEDS_CLARIFICATION, InterpretationStatus.PROVIDER_FAILED}
               for turn in self.turns[:-1]):
            raise ValueError("a closed interpretation cannot have further turns")
        latest = self.turns[-1]
        if (latest.status != self.status or latest.decision_id != self.decision_id
                or latest.questions != self.questions):
            raise ValueError("interpretation summary differs from its latest turn")
        for turn in self.turns:
            if turn.action_request and (turn.action_request.agent_id, turn.action_request.actor_role) != (self.agent_id, self.actor_role):
                raise ValueError("a turn cannot change the original caller identity")
        return self


class RequestInterpretation(SQLModel, table=True):
    __tablename__ = 'request_interpretations'

    interpretation_id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    agent_id: str = Field(index=True)
    actor_role: str
    original_prompt: str
    status: str = Field(sa_column=Column(
        String, enum_check('status', InterpretationStatus, 'ck_interpretation_status'), nullable=False,
    ))
    revision: int = 1
    turns: list[dict] = Field(default_factory=list, sa_column=Column(JSONType, nullable=False))
    questions: list[str] = Field(default_factory=list, sa_column=Column(JSONType, nullable=False))
    decision_id: str | None = Field(default=None, sa_column=Column(
        String, ForeignKey('decision_records.decision_id', ondelete='RESTRICT'),
        nullable=True, index=True,
    ))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
