"""
One-time backfill: populate compiled_output for completed jobs
that predate the compiled_output column addition.

Run via:
  docker exec scaffold-orchestrator python3 /app/scripts/backfill_compiled_output.py

Idempotent: safe to run multiple times — only touches rows where
status='completed' AND compiled_output IS NULL.
"""

import asyncio
import logging
import sys
import time

# ── Bootstrap ────────────────────────────────────────────────────────
# Ensure /app is on sys.path so imports resolve inside the container.
sys.path.insert(0, "/app")

from sqlalchemy import text
from app.database import async_session
from app.modules.execution_agent import _compile_output

logger = logging.getLogger("backfill_compiled_output")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


async def main() -> None:
    t_start = time.monotonic()
    backfilled = 0
    skipped_no_done = 0
    errors = 0

    # ── 1. Discover candidate jobs ───────────────────────────────────
    async with async_session() as db:
        result = await db.execute(
            text(
                "SELECT id FROM jobs "
                "WHERE status = 'completed' AND compiled_output IS NULL "
                "ORDER BY created_at"
            )
        )
        job_ids = [str(row[0]) for row in result.fetchall()]

    logger.info("Found %s completed jobs with NULL compiled_output", len(job_ids))

    if not job_ids:
        logger.info("Nothing to backfill — exiting")
        return

    # ── 2. Process each job in its own session ───────────────────────
    for i, job_id in enumerate(job_ids, 1):
        async with async_session() as db:
            try:
                # Check if this job has any done nodes at all
                done_count_row = await db.execute(
                    text(
                        "SELECT COUNT(*) FROM dag_nodes "
                        "WHERE job_id = :jid AND status = 'done'"
                    ),
                    {"jid": job_id},
                )
                done_count = done_count_row.scalar()

                if done_count == 0:
                    logger.info(
                        "[%s/%s] job %s — skipped (0 done nodes)",
                        i, len(job_ids), job_id,
                    )
                    skipped_no_done += 1
                    continue

                # Check for missing dag_nodes entirely
                total_row = await db.execute(
                    text(
                        "SELECT COUNT(*) FROM dag_nodes WHERE job_id = :jid"
                    ),
                    {"jid": job_id},
                )
                total_nodes = total_row.scalar()

                if total_nodes == 0:
                    logger.warning(
                        "[%s/%s] job %s — skipped (no dag_nodes rows)",
                        i, len(job_ids), job_id,
                    )
                    skipped_no_done += 1
                    continue

                # Call the existing compiler
                compiled = await _compile_output(job_id, db)

                if compiled:
                    await db.execute(
                        text(
                            "UPDATE jobs SET compiled_output = :co, "
                            "updated_at = NOW() "
                            "WHERE id = :jid AND compiled_output IS NULL"
                        ),
                        {"co": compiled, "jid": job_id},
                    )
                    await db.commit()
                    logger.info(
                        "[%s/%s] job %s — backfilled (%s chars)",
                        i, len(job_ids), job_id, len(compiled),
                    )
                    backfilled += 1
                else:
                    logger.info(
                        "[%s/%s] job %s — skipped (_compile_output returned empty)",
                        i, len(job_ids), job_id,
                    )
                    skipped_no_done += 1

            except Exception:
                logger.exception(
                    "[%s/%s] job %s — ERROR during backfill",
                    i, len(job_ids), job_id,
                )
                errors += 1
                # Roll back this job's session, continue with next
                await db.rollback()

    # ── 3. Summary ───────────────────────────────────────────────────
    elapsed = time.monotonic() - t_start
    logger.info(
        "Backfill complete: %s jobs backfilled, %s skipped (no done nodes), "
        "%s errors, %.1fs elapsed",
        backfilled, skipped_no_done, errors, elapsed,
    )


if __name__ == "__main__":
    asyncio.run(main())
