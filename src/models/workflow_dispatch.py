"""Transactional outbox records for reliable Celery publication."""
from datetime import datetime, timezone
from enum import Enum
from pydantic import Field as ContractField, model_validator

from sqlalchemy import CheckConstraint, Column, Index, String, UniqueConstraint
from sqlmodel import Field, SQLModel

from src.models.constraints import enum_check
from src.models.request import ValidatedContract
from src.models.types import JSONType


class DispatchStatus(str, Enum):
    PENDING = 'PENDING'
    PUBLISHING = 'PUBLISHING'
    PUBLISHED = 'PUBLISHED'
    CANCELLED = 'CANCELLED'


class WorkflowDispatchWrite(ValidatedContract):
    dispatch_id: str = ContractField(pattern=r'^[0-9a-f]{64}$')
    operation_id: str = ContractField(pattern=r'^[0-9a-f]{64}$')
    generation: int = ContractField(ge=1, strict=True)
    status: DispatchStatus
    publisher_owner: str | None = None
    publisher_lease_until: datetime | None = None
    publish_attempts: int = ContractField(default=0, ge=0, strict=True)
    available_at: datetime
    published_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode='after')
    def publisher_lease(self):
        if self.status == DispatchStatus.PUBLISHING:
            if not self.publisher_owner or self.publisher_lease_until is None:
                raise ValueError('publishing dispatch requires a publisher lease')
        elif self.publisher_owner is not None or self.publisher_lease_until is not None:
            raise ValueError('non-publishing dispatch cannot retain a publisher lease')
        if self.status == DispatchStatus.PUBLISHED and self.published_at is None:
            raise ValueError('published dispatch requires its publication timestamp')
        return self


class WorkflowDispatch(SQLModel, table=True):
    __tablename__ = 'workflow_dispatches'

    dispatch_id: str = Field(primary_key=True)
    operation_id: str = Field(foreign_key='workflow_operations.operation_id', index=True)
    generation: int = Field(ge=1)
    status: str = Field(sa_column=Column(String, enum_check('status', DispatchStatus, 'ck_workflow_dispatch_status'), nullable=False))
    publisher_owner: str | None = None
    publisher_lease_until: datetime | None = None
    publish_attempts: int = Field(default=0, ge=0)
    available_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), index=True)
    published_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint('operation_id', 'generation', name='uq_workflow_dispatch_generation'),
        Index('ix_workflow_dispatch_due', 'status', 'available_at'),
        CheckConstraint('generation >= 1 AND publish_attempts >= 0', name='ck_workflow_dispatch_counts'),
        CheckConstraint("status <> 'PUBLISHED' OR published_at IS NOT NULL", name='ck_workflow_dispatch_published'),
        CheckConstraint("(status = 'PUBLISHING' AND publisher_owner IS NOT NULL AND publisher_lease_until IS NOT NULL) "
                        "OR (status <> 'PUBLISHING' AND publisher_owner IS NULL AND publisher_lease_until IS NULL)",
                        name='ck_workflow_dispatch_publisher_lease'),
    )


def save_dispatch(session, row: WorkflowDispatch) -> WorkflowDispatch:
    """Validate the DB write contract before adding a dispatch intent."""
    validated = WorkflowDispatchWrite.model_validate(row.model_dump())
    for key, value in validated.model_dump(mode='python').items():
        setattr(row, key, value.value if isinstance(value, DispatchStatus) else value)
    session.add(row)
    session.flush()
    return row
