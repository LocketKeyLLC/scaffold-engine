"""llm_call_logs.error — why a call failed (§17.1139, ledger L-7)

Revision ID: 0004_llm_call_logs_error
Revises: 0003_turn_runs_session_fk
Create Date: 2026-09-20

The call log recorded ``success=false`` and a latency and nothing else; the
provider's rejection text lived only in the container log. Ledger L-7
("gemma4 fails 20 % of calls") could be attributed to the past-due Ollama
Cloud account only from memory, and the 244 zero-latency failures on
deepseek since 09-10 could not be classified at all. The first 300 chars of
the driver/provider error now ride the row, so the provider-rejection alert
can say WHAT the provider said.
"""
from __future__ import annotations

from alembic import op

revision = "0004_llm_call_logs_error"
down_revision = "0003_turn_runs_session_fk"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE llm_call_logs ADD COLUMN IF NOT EXISTS error TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE llm_call_logs DROP COLUMN IF EXISTS error")
