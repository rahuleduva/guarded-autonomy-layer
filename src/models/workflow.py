"""Durable processing state, distinct from a completed policy decision."""
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from pydantic import Field as ContractField, model_validator
from sqlalchemy import Column, String, Integer, UniqueConstraint, CheckConstraint, JSON, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import SQLModel, Field
from src.models.constraints import enum_check
from src.models.request import ValidatedContract
from src.models.types import JSONType


NullableJSON = JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), 'postgresql')


class WorkflowStatus(str, Enum):
    QUEUED = 'QUEUED'
    RETRY_WAIT = 'RETRY_WAIT'
    PROCESSING = 'PROCESSING'
    COMPLETED = 'COMPLETED'
    FAILED = 'FAILED'


class StoredWorkflowResponse(ValidatedContract):
    encrypted: str = ContractField(min_length=40)


class StoredWorkflowInput(ValidatedContract):
    encrypted: str = ContractField(min_length=40)


class WorkflowFailure(ValidatedContract):
    error_type: str = ContractField(min_length=1)
    http_status: int = ContractField(ge=400, le=599, strict=True)
    detail: Any
    error_message: str | None = None
    exception_chain: list[dict[str, str]] = ContractField(default_factory=list)


class WorkflowWrite(ValidatedContract):
    operation_id: str
    caller_principal_id: str | None = None
    kind: str = ContractField(min_length=1)
    scope: str = ContractField(min_length=1)
    idempotency_key: str = ContractField(min_length=1, max_length=200)
    input_hash: str = ContractField(pattern=r'^[0-9a-f]{64}$')
    status: WorkflowStatus
    stage: str = ContractField(min_length=1)
    owner: str | None = None
    lease_until: datetime | None = None
    checkpoint: dict[str, Any] = ContractField(default_factory=dict)
    response: StoredWorkflowResponse | None = None
    error: WorkflowFailure | None = None
    retryable: bool = False
    execution_mode: Literal['sync', 'background'] = 'sync'
    input_envelope: StoredWorkflowInput | None = None
    generation: int = ContractField(default=1, ge=1, strict=True)
    run_count: int = ContractField(default=0, ge=0, strict=True)
    automatic_retry_count: int = ContractField(default=0, ge=0, strict=True)
    next_run_at: datetime | None = None
    queue_deadline: datetime | None = None
    run_deadline: datetime | None = None
    heartbeat_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode='after')
    def state_contract(self):
        if self.status == WorkflowStatus.COMPLETED:
            if self.response is None or self.error is not None or self.retryable:
                raise ValueError('completed operation requires response and no failure')
        elif self.status == WorkflowStatus.PROCESSING:
            if self.response is not None or self.error is not None or self.retryable:
                raise ValueError('processing operation cannot claim a final result')
            if not self.owner or self.lease_until is None:
                raise ValueError('processing operation requires an owner and lease')
        elif self.status in {WorkflowStatus.QUEUED, WorkflowStatus.RETRY_WAIT}:
            if (self.response is not None or self.owner is not None
                    or self.lease_until is not None):
                raise ValueError('waiting operation cannot have a result or owner')
            if self.execution_mode != 'background' or self.input_envelope is None:
                raise ValueError('queued operation requires a frozen background input')
            if self.status == WorkflowStatus.QUEUED and (self.error is not None or self.retryable):
                raise ValueError('queued operation cannot retain an active failure')
            if self.status == WorkflowStatus.RETRY_WAIT and (self.error is None or not self.retryable):
                raise ValueError('retry wait requires its preceding retryable failure')
        elif self.error is None or self.response is not None:
            raise ValueError('failed operation requires error and no response')
        if self.execution_mode == 'background':
            if self.input_envelope is None or not self.caller_principal_id:
                raise ValueError('background operation requires frozen input and caller ownership')
            if self.status == WorkflowStatus.QUEUED:
                if self.queue_deadline is None or self.next_run_at is not None:
                    raise ValueError('queued operation requires a queue deadline and no retry time')
            elif self.status == WorkflowStatus.RETRY_WAIT:
                if self.next_run_at is None or self.queue_deadline is not None:
                    raise ValueError('retry wait requires a retry time and no queue deadline')
            elif self.status == WorkflowStatus.PROCESSING:
                if (self.run_deadline is None or self.heartbeat_at is None
                        or self.queue_deadline is not None or self.next_run_at is not None):
                    raise ValueError('background processing requires run deadline/heartbeat and no queue schedule')
                utc = lambda value: value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
                if utc(self.lease_until) > utc(self.run_deadline):
                    raise ValueError('worker lease cannot extend past its run deadline')
            if self.status != WorkflowStatus.PROCESSING and any(value is not None for value in (
                    self.owner, self.lease_until, self.run_deadline, self.heartbeat_at)):
                raise ValueError('non-processing background operation cannot retain worker ownership')
            if self.status in {WorkflowStatus.COMPLETED, WorkflowStatus.FAILED} and (
                    self.queue_deadline is not None or self.next_run_at is not None):
                raise ValueError('terminal background operation cannot retain an active schedule')
        return self


class WorkflowProgress(ValidatedContract):
    status: Literal['queued', 'processing', 'retry_wait']
    operation_id: str
    stage: str
    status_url: str
    result_url: str


class WorkflowOperation(SQLModel, table=True):
    __tablename__ = 'workflow_operations'
    operation_id: str = Field(primary_key=True)
    caller_principal_id: str | None = Field(default=None, foreign_key='auth_principals.principal_id', index=True)
    kind: str
    scope: str
    idempotency_key: str
    input_hash: str
    status: str = Field(sa_column=Column(String, enum_check('status', WorkflowStatus, 'ck_workflow_status'), nullable=False))
    stage: str
    owner: str | None = None
    lease_until: datetime | None = None
    checkpoint: dict = Field(default_factory=dict, sa_column=Column(JSONType, nullable=False))
    response: dict | None = Field(default=None, sa_column=Column(NullableJSON, nullable=True))
    error: dict | None = Field(default=None, sa_column=Column(NullableJSON, nullable=True))
    retryable: bool = False
    # Background execution fields. The API feature flag remains off until the
    # operator generates, reviews, and applies the corresponding Alembic change.
    execution_mode: str = Field(default='sync', sa_column=Column(String, nullable=False, server_default='sync'))
    input_envelope: dict | None = Field(default=None, sa_column=Column(NullableJSON, nullable=True))
    generation: int = Field(default=1, sa_column=Column(Integer, nullable=False, server_default='1'))
    run_count: int = Field(default=0, sa_column=Column(Integer, nullable=False, server_default='0'))
    automatic_retry_count: int = Field(default=0, sa_column=Column(Integer, nullable=False, server_default='0'))
    next_run_at: datetime | None = Field(default=None, nullable=True)
    queue_deadline: datetime | None = Field(default=None, nullable=True)
    run_deadline: datetime | None = Field(default=None, nullable=True)
    heartbeat_at: datetime | None = Field(default=None, nullable=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index('ix_workflow_status_lease', 'status', 'lease_until'),
        Index('ix_workflow_operation_due', 'status', 'next_run_at'),
        Index('ix_workflow_queue_deadline', 'status', 'queue_deadline'),
        Index('ix_workflow_run_deadline', 'status', 'run_deadline'),
        UniqueConstraint('kind', 'scope', 'idempotency_key', name='uq_workflow_identity'),
        CheckConstraint("execution_mode IN ('sync', 'background')", name='ck_workflow_execution_mode'),
        CheckConstraint("length(trim(stage)) > 0 AND (owner IS NULL OR length(trim(owner)) > 0)", name='ck_workflow_owner_stage'),
        CheckConstraint("length(trim(kind)) > 0 AND length(trim(scope)) > 0 "
                        "AND length(trim(idempotency_key)) BETWEEN 1 AND 200 "
                        "AND length(input_hash) = 64", name='ck_workflow_identity'),
        CheckConstraint('generation >= 1 AND run_count >= 0 AND automatic_retry_count >= 0',
                        name='ck_workflow_counters'),
        CheckConstraint("(status = 'QUEUED' AND execution_mode = 'background' "
                        "AND input_envelope IS NOT NULL AND response IS NULL AND error IS NULL "
                        "AND owner IS NULL AND lease_until IS NULL AND retryable = false) "
                        "OR (status = 'RETRY_WAIT' AND execution_mode = 'background' "
                        "AND input_envelope IS NOT NULL AND response IS NULL AND error IS NOT NULL "
                        "AND owner IS NULL AND lease_until IS NULL AND retryable = true) "
                        "OR (status = 'PROCESSING' AND response IS NULL AND error IS NULL AND retryable = false AND owner IS NOT NULL AND lease_until IS NOT NULL) "
                        "OR (status = 'COMPLETED' AND response IS NOT NULL AND error IS NULL AND retryable = false) "
                        "OR (status = 'FAILED' AND response IS NULL AND error IS NOT NULL)", name='ck_workflow_result'),
        CheckConstraint("execution_mode = 'sync' OR (input_envelope IS NOT NULL "
            "AND caller_principal_id IS NOT NULL AND ("
            "(status = 'PROCESSING' AND run_deadline IS NOT NULL AND heartbeat_at IS NOT NULL "
            "AND lease_until <= run_deadline AND queue_deadline IS NULL AND next_run_at IS NULL) OR "
            "(status <> 'PROCESSING' AND owner IS NULL AND lease_until IS NULL "
            "AND run_deadline IS NULL AND heartbeat_at IS NULL AND ("
            "(status = 'QUEUED' AND queue_deadline IS NOT NULL AND next_run_at IS NULL) OR "
            "(status = 'RETRY_WAIT' AND queue_deadline IS NULL AND next_run_at IS NOT NULL) OR "
            "(status IN ('COMPLETED', 'FAILED') AND queue_deadline IS NULL AND next_run_at IS NULL)))))",
            name='ck_workflow_background_schedule'),
    )
