"""assist_turn_runs.session_id → assist_sessions(id) ON DELETE CASCADE (§17.1128)

Revision ID: 0003_turn_runs_session_fk
Revises: 0002_turn_run_timings
Create Date: 2026-09-19

The table (SQL migration 071, §17.1007) was created with a bare ``session_id
UUID NOT NULL`` — no foreign key — so deleting an assist session (or its job,
which cascades to the session) left the session's turn runs behind: the
§17.1127 purge found 11 + 100 orphaned timing rows from 20 deleted sessions.
Orphans are removed first so the constraint can be added on any deployment.
"""
from __future__ import annotations

from alembic import op

revision = "0003_turn_runs_session_fk"
down_revision = "0002_turn_run_timings"
branch_labels = None
depends_on = None

CONSTRAINT = "assist_turn_runs_session_id_fkey"


def upgrade() -> None:
    op.execute(
        "DELETE FROM assist_turn_runs r WHERE NOT EXISTS "
        "(SELECT 1 FROM assist_sessions s WHERE s.id = r.session_id)"
    )
    op.execute(f"ALTER TABLE assist_turn_runs DROP CONSTRAINT IF EXISTS {CONSTRAINT}")
    op.execute(
        f"ALTER TABLE assist_turn_runs ADD CONSTRAINT {CONSTRAINT} "
        "FOREIGN KEY (session_id) REFERENCES assist_sessions(id) ON DELETE CASCADE"
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE assist_turn_runs DROP CONSTRAINT IF EXISTS {CONSTRAINT}")
