"""Add enum checks omitted by initial schema autogeneration.

Migration values are frozen deliberately; model enums may evolve later.
"""
from alembic import op

revision = "e21b459fc806"
down_revision = "d764629ec013"
branch_labels = None
depends_on = None

CHECKS = {
    "autonomy_state": [("ck_autonomy_mode", "mode IN ('SHADOW', 'ASSISTED', 'LIVE')")],
    "decision_records": [
        ("ck_decision_risk_level", "risk_level IN ('READ_ONLY', 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL')"),
        ("ck_decision_autonomy_level", "autonomy_level IN ('SHADOW', 'ASSISTED', 'LIVE')"),
        ("ck_decision_outcome", "outcome IN ('ALLOWED', 'DENIED', 'ESCALATED', 'PENDING_APPROVAL', 'SHADOW_LOGGED')"),
    ],
    "escalation_queue": [("ck_escalation_status", "status IN ('PENDING', 'APPROVED', 'REJECTED')")],
    "shadow_records": [("ck_shadow_would_have_decided", "would_have_decided IN ('ALLOWED', 'DENIED', 'ESCALATED', 'PENDING_APPROVAL', 'SHADOW_LOGGED')")],
}


def upgrade():
    for table, constraints in CHECKS.items():
        with op.batch_alter_table(table) as batch:
            for name, expression in constraints:
                batch.create_check_constraint(name, expression)


def downgrade():
    for table, constraints in reversed(list(CHECKS.items())):
        with op.batch_alter_table(table) as batch:
            for name, _ in constraints:
                batch.drop_constraint(name, type_="check")
