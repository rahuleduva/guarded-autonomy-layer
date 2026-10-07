"""Persistent review and simulated execution tables."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "d764629ec013"
down_revision = "c85a51d74e02"
branch_labels = None
depends_on = None


def upgrade():
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table("policy_promotion_events",
        sa.Column("event_id", sa.String(), primary_key=True),
        sa.Column("policy_hash", sa.String(), sa.ForeignKey("policies.policy_hash", ondelete="RESTRICT"), nullable=False),
        sa.Column("reviewer_id", sa.String(), nullable=False),
        sa.Column("approved", sa.Boolean(), nullable=False),
        sa.Column("promoted", sa.Boolean(), nullable=False),
        sa.Column("evidence_json", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("simulated_resources",
        sa.Column("resource", sa.String(), primary_key=True),
        sa.Column("value_json", json_type, nullable=False))
    op.create_table("execution_records",
        sa.Column("execution_id", sa.String(), primary_key=True),
        sa.Column("decision_id", sa.String(), sa.ForeignKey("decision_records.decision_id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("before_state", json_type, nullable=False),
        sa.Column("after_state", json_type, nullable=False),
        sa.Column("compensation_decision_id", sa.String(), sa.ForeignKey("decision_records.decision_id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))


def downgrade():
    op.drop_table("execution_records")
    op.drop_table("simulated_resources")
    op.drop_table("policy_promotion_events")
