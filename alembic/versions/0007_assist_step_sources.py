"""assist_step_sources: the web pages a step's research found, kept (§17.1437)

Revision ID: 0007_assist_step_sources
Revises: 0006_runner_secrets
Create Date: 2026-10-09

ADD4's router walkthrough asked the operator to describe the My Spectrum app
three times across §17.1435-1436, and a fourth time after both fixes shipped.
Each pass researched from scratch, and the web search behind it is the least
reliable link in the engine: on 2026-10-09 google cse, brave and startpage were
suspended and bing returned only homepages, so the pass had no source and fell
back to asking. A page that answered the step on one pass was gone on the next.

This table keeps what a step's research found — per session and step, keyed by
URL — so a later pass of the same step reads it back whether or not search
answers that minute. No foreign key: rows are written inside the caller's
transaction while it may hold the session row FOR UPDATE.
"""
from __future__ import annotations

from alembic import op

revision = "0007_assist_step_sources"
down_revision = "0006_runner_secrets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS assist_step_sources (
            session_id  UUID NOT NULL,
            node_key    TEXT NOT NULL,
            url         TEXT NOT NULL,
            title       TEXT NOT NULL DEFAULT '',
            body        TEXT NOT NULL,
            published   TEXT NOT NULL DEFAULT '',
            query       TEXT NOT NULL DEFAULT '',
            found_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (session_id, node_key, url)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS assist_step_sources")
