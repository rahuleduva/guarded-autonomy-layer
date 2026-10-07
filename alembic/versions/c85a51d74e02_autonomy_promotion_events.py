"""Record autonomy promotion evidence and reviewer decisions.

Revision ID: c85a51d74e02
Revises: 8696a72fef7b
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c85a51d74e02"
down_revision = "8696a72fef7b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "autonomy_promotion_events",
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("autonomy_state_id", sa.Integer(), nullable=False),
        sa.Column("previous_mode", sa.String(), nullable=False),
        sa.Column("requested_mode", sa.String(), nullable=False),
        sa.Column("reviewer_id", sa.String(), nullable=True),
        sa.Column("approved", sa.Boolean(), nullable=False),
        sa.Column("promoted", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("evidence_json", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("previous_mode IN ('SHADOW', 'ASSISTED', 'LIVE')", name="ck_promotion_previous_mode"),
        sa.CheckConstraint("requested_mode IN ('SHADOW', 'ASSISTED', 'LIVE')", name="ck_promotion_requested_mode"),
        sa.ForeignKeyConstraint(["autonomy_state_id"], ["autonomy_state.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index("ix_autonomy_promotion_events_autonomy_state_id", "autonomy_promotion_events", ["autonomy_state_id"])


def downgrade() -> None:
    op.drop_index("ix_autonomy_promotion_events_autonomy_state_id", table_name="autonomy_promotion_events")
    op.drop_table("autonomy_promotion_events")
