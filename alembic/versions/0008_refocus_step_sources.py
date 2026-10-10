"""assist_step_sources: drop pages kept as their first 2000 characters (§17.1443)

Revision ID: 0008_refocus_step_sources
Revises: 0007_assist_step_sources
Create Date: 2026-10-10

Until §17.1443 a fetched page reached assist research as `text[:2000]`, and
§17.1437 kept exactly that. For ADD4's PureVPN page that was the article's
"what is port forwarding" intro; the My Spectrum app steps at char ~8,000 were
never kept, and every later pass recalled the intro instead of the steps.
Research now keeps a page's stretches about its query. The rows kept before
are the heads, so they are deleted: the next pass of each step re-keeps the
page focused, and a recall never serves an intro as a source again.
"""
from __future__ import annotations

from alembic import op

revision = "0008_refocus_step_sources"
down_revision = "0007_assist_step_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM assist_step_sources")


def downgrade() -> None:
    pass
