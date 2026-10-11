"""runner_secrets: a login the engine holds FOR the operator can be shown to them (§17.1463)

Revision ID: 0009_runner_secrets_for_operator
Revises: 0008_refocus_step_sources
Create Date: 2026-10-11

The store was write-only by design: "a value never comes back out of this API". That is right for a service's
API key and wrong for a login the operator has to type: ADD127 put the control panel behind Basic auth with the
runner secret PANEL_PASSWORD (2026-10-08), the panel worked, and nobody could log in to it — the operator asked
"the engine never gave the user the username and password it set up … How are they suppose to log in?".
`for_operator` marks a value that is the operator's own login; only those can be revealed, and each reveal is
recorded (`revealed_at`, `reveal_count`). Existing rows whose hint says they are a login are marked.
"""
from __future__ import annotations

from alembic import op

revision = "0009_runner_secrets_for_operator"
down_revision = "0008_refocus_step_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE runner_secrets ADD COLUMN IF NOT EXISTS for_operator BOOLEAN NOT NULL DEFAULT false")
    op.execute("ALTER TABLE runner_secrets ADD COLUMN IF NOT EXISTS revealed_at TIMESTAMPTZ")
    op.execute("ALTER TABLE runner_secrets ADD COLUMN IF NOT EXISTS reveal_count INTEGER NOT NULL DEFAULT 0")
    op.execute("UPDATE runner_secrets SET for_operator = true WHERE hint ~* '\\mlogin\\M'")


def downgrade() -> None:
    op.execute("ALTER TABLE runner_secrets DROP COLUMN IF EXISTS reveal_count")
    op.execute("ALTER TABLE runner_secrets DROP COLUMN IF EXISTS revealed_at")
    op.execute("ALTER TABLE runner_secrets DROP COLUMN IF EXISTS for_operator")
