"""Shared job-state helpers."""
from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.job_state import ERROR_SUMMARY_MAX as _ERROR_SUMMARY_MAX, transition

logger = logging.getLogger("scaffold.jobs")


async def fail_job(db: AsyncSession, job_id: UUID | str, error: str) -> bool:
    """Mark job failed with truncated error_summary. Commits.

    §17.1107 (ledger S-2) — guarded: a job already ``completed``/``cancelled``/
    ``failed`` is left alone (14 callers; a late DAG/Phase-2 failure after an
    operator cancel used to flip ``cancelled → failed`` and rewrite the
    summary). Returns True iff the row moved.
    """
    prior = await transition(
        db, job_id, to="failed", set_columns={"error_summary": error}, reason="fail_job",
    )
    await db.commit()
    if prior is None:
        return False
    logger.error("job_failed: job=%s from=%s error=%s", job_id, prior, error)
    return True
