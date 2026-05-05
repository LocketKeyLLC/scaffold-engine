"""Shared job-state helpers."""
from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("scaffold.jobs")

_ERROR_SUMMARY_MAX = 1000


async def fail_job(db: AsyncSession, job_id: UUID | str, error: str) -> None:
    """Mark job failed with truncated error_summary. Commits."""
    await db.execute(
        text("UPDATE jobs SET status = 'failed', error_summary = :error WHERE id = :id"),
        {"error": error[:_ERROR_SUMMARY_MAX], "id": job_id},
    )
    await db.commit()
    logger.error("job_failed: job=%s error=%s", job_id, error)
