"""assist_turn_runs.timings — per-stage timing record for one turn (§17.1109)

Revision ID: 0002_turn_run_timings
Revises: 0001_baseline
Create Date: 2026-09-19

Phase 1 ledger L-1: turns took p50 74 s with 31 s outside any model call and
nothing durable recorded where. The turn driver now writes
``{"total_ms", "llm_ms", "llm_calls", "non_llm_ms", "stages": [{"label",
"at_ms", "ms", "llm_ms", "llm_calls", "non_llm_ms"}, …]}`` here at finalize;
``scripts/turn_timing_report.py`` aggregates it.
"""
from __future__ import annotations

from alembic import op

revision = "0002_turn_run_timings"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE assist_turn_runs ADD COLUMN IF NOT EXISTS timings JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE assist_turn_runs DROP COLUMN IF EXISTS timings")
