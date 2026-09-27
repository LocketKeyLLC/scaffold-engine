"""jobs.status gains 'awaiting_decision' (§17.1184)

Revision ID: 0005_awaiting_decision_status
Revises: 0004_llm_call_logs_error
Create Date: 2026-09-27

The autonomous executor now stops at a `decision` node and asks the operator
instead of letting the model choose for them. The job waits in
'awaiting_decision' (nodes untouched) until POST /jobs/{id}/decide records
the answer and restarts the run. Same shape as 055_awaiting_assist_status.sql:
drop + re-add the CHECK with the new member.
"""
from __future__ import annotations

from alembic import op

revision = "0005_awaiting_decision_status"
down_revision = "0004_llm_call_logs_error"
branch_labels = None
depends_on = None

_BASE = (
    "'pending', 'refining', 'awaiting_confirmation', 'researching', "
    "'planning', 'executing', 'running', 'completed', 'failed', "
    "'cancelled', 'blocked', 'assisted_executing', 'assisted_running', "
    "'assisted_paused', 'aggregating', 'awaiting_assist'"
)


def upgrade() -> None:
    op.execute(
        "ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_status_check, "
        "ADD CONSTRAINT jobs_status_check CHECK (status = ANY (ARRAY["
        + _BASE + ", 'awaiting_decision']))"
    )


def downgrade() -> None:
    op.execute("UPDATE jobs SET status = 'executing' WHERE status = 'awaiting_decision'")
    op.execute(
        "ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_status_check, "
        "ADD CONSTRAINT jobs_status_check CHECK (status = ANY (ARRAY[" + _BASE + "]))"
    )
