"""Add independent, sanitized request-attempt monitoring events."""
from alembic import op
import sqlalchemy as sa

revision = "f3c18b6a42d7"
down_revision = "e21b459fc806"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "request_attempt_events",
        sa.Column("event_id", sa.String(), primary_key=True),
        sa.Column("attempt_id", sa.String(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("route", sa.String(), nullable=False),
        sa.Column("stage", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("provider", sa.String(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("error_type", sa.String(), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("decision_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_request_attempt_events_attempt_id", "request_attempt_events", ["attempt_id"])
    op.create_index("ix_request_attempt_events_decision_id", "request_attempt_events", ["decision_id"])


def downgrade() -> None:
    op.drop_index("ix_request_attempt_events_decision_id", table_name="request_attempt_events")
    op.drop_index("ix_request_attempt_events_attempt_id", table_name="request_attempt_events")
    op.drop_table("request_attempt_events")
