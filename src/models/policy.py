# Policy artifact & SQLModel table
import datetime
from typing import Any, Dict, Optional

from sqlmodel import Field, SQLModel
from sqlalchemy import Index, text, CheckConstraint, Column
from sqlalchemy.dialects.postgresql import BOOLEAN
from src.models.types import JSONType


class Policy(SQLModel, table=True):
    __tablename__ = "policies"

    id: Optional[int] = Field(default=None, primary_key=True)
    version: str = Field(index=True)
    policy_hash: str = Field(unique=True, index=True)
    rules_json: Dict[str, Any] = Field(..., sa_type=JSONType)
    is_active: bool = Field(default=False)
    is_shadow: bool = Field(default=False, index=True)
    activated_at: Optional[datetime.datetime] = Field(default=None)
    deactivated_at: Optional[datetime.datetime] = Field(default=None)
    reviewer_id: Optional[str] = Field(default=None)
    created_at: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc))

    __table_args__ = (
        Index(
            "uq_policies_one_active",
            "is_active",
            unique=True,
            postgresql_where=text("is_active"),
            sqlite_where=text("is_active"),
        ),
        Index(
            "uq_policies_one_shadow",
            "is_shadow",
            unique=True,
            postgresql_where=text("is_shadow"),
            sqlite_where=text("is_shadow"),
        ),
        CheckConstraint(
            "(is_active AND activated_at IS NOT NULL AND deactivated_at IS NULL) OR "
            "(NOT is_active AND ((activated_at IS NULL AND deactivated_at IS NULL) OR "
            "(activated_at IS NULL AND deactivated_at IS NOT NULL)))",
            name="ck_policies_activation_invariant",
        ),
    )