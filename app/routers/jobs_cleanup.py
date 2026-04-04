"""
Task 2: Stale Job Cleanup — POST /jobs/cleanup + optional startup hook

Drop this file into: ~/scaffold-engine/app/routers/jobs_cleanup.py

Integration (in app/main.py):
    from app.routers.jobs_cleanup import router as cleanup_router, run_cleanup_if_enabled
    app.include_router(cleanup_router)

    # Add the startup hook (after app = FastAPI(...)):
    @app.on_event("startup")
    async def startup_cleanup():
        await run_cleanup_if_enabled()

    # Or if using lifespan (FastAPI ≥0.95):
    # Add `await run_cleanup_if_enabled()` inside your lifespan async generator.

Files to examine first:
    - app/main.py — confirm how the DB session/engine is obtained
    - app/settings.py or app/config.py — confirm DATABASE_URL and auth pattern
    - Check the jobs table schema:
        docker exec scaffold-postgres psql -U scaffold -c "\d jobs"
      Verify column names: status, updated_at (or modified_at), compiled_output

Authentication:
    Uses the same X-API-Key header pattern as other endpoints.
    Verify the auth dependency name in your codebase — this file assumes
    a Depends() callable named `verify_api_key` exists. Adjust the import.

Verification:
    # Create a stale job for testing:
    docker exec scaffold-postgres psql -U scaffold -c "
      INSERT INTO jobs (id, status, updated_at, compiled_output)
      VALUES ('test-stale-001', 'running', NOW() - INTERVAL '45 minutes', NULL)
      ON CONFLICT (id) DO UPDATE SET status='running', updated_at=NOW()-INTERVAL '45 minutes';
    "

    # Run cleanup:
    curl -s -X POST http://localhost:8000/jobs/cleanup \
      -H "X-API-Key: sk-scaffold-89dd4e24beb2ce03b3ba441880486c35fe45e1c717e2d474" \
      | python3 -m json.tool

    # Expected:
    # {"cleaned": {"running_to_failed": 1, "planning_to_cancelled": 0}, "timestamp": "..."}

    # Verify the job was updated:
    docker exec scaffold-postgres psql -U scaffold -c "SELECT id, status, compiled_output FROM jobs WHERE id='test-stale-001';"

    # Test startup hook:
    CLEANUP_ON_STARTUP=true docker compose up -d --build scaffold-orchestrator
    docker logs scaffold-orchestrator 2>&1 | grep stale_job_cleaned

Rollback:
    Remove the file, remove include_router() and startup hook from main.py.
"""

import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import text

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["ops"])


# ── Database session dependency ──────────────────────────────────────
# IMPORTANT: Replace this import with wherever your project gets its
# async DB session.  Common patterns seen in scaffold-engine:
#
#   from app.database import get_db          # async generator yielding AsyncSession
#   from app.dependencies import get_db
#   from app.main import get_db
#
# Adjust the import below to match your codebase.

try:
    from app.database import get_db  # type: ignore
except ImportError:
    # Fallback — define a placeholder so the file parses.
    # You MUST replace this with the real session factory.
    async def get_db():  # type: ignore
        raise RuntimeError("Replace get_db import in jobs_cleanup.py")
        yield  # noqa

# ── Auth dependency ──────────────────────────────────────────────────
# Same pattern: import from wherever your project defines API key auth.
try:
    from app.auth import verify_api_key  # type: ignore
except ImportError:
    try:
        from app.dependencies import verify_api_key  # type: ignore
    except ImportError:
        from fastapi import Header, HTTPException

        _SCAFFOLD_API_KEY = os.getenv("SCAFFOLD_API_KEY", "")

        async def verify_api_key(x_api_key: str = Header(...)):  # type: ignore
            if not _SCAFFOLD_API_KEY or x_api_key != _SCAFFOLD_API_KEY:
                raise HTTPException(status_code=401, detail="Invalid API key")


# ── Core cleanup logic ───────────────────────────────────────────────

async def _do_cleanup(db) -> dict:
    """
    Find and resolve stale jobs. Returns counts.

    Stale definitions:
      - running  + updated_at > 30 min ago  → failed (timed out)
      - planning + updated_at > 60 min ago  → cancelled
    """
    now = datetime.now(timezone.utc)

    # ── Running → Failed ─────────────────────────────────────────
    stale_running = await db.execute(
        text("""
            SELECT id, updated_at
            FROM jobs
            WHERE status = 'running'
              AND updated_at < NOW() - INTERVAL '30 minutes'
        """)
    )
    running_rows = stale_running.fetchall()

    for row in running_rows:
        job_id = row[0]
        updated_at = row[1]
        age_minutes = round((now - updated_at.replace(tzinfo=timezone.utc)).total_seconds() / 60, 1)

        await db.execute(
            text("""
                UPDATE jobs
                SET status = 'failed',
                    compiled_output = :msg,
                    updated_at = NOW()
                WHERE id = :jid
            """),
            {"jid": str(job_id), "msg": "Job timed out after 30 minutes of inactivity"},
        )
        logger.info(
            'event="stale_job_cleaned" job_id=%s old_status=running new_status=failed age_minutes=%s',
            job_id, age_minutes,
        )

    # ── Planning → Cancelled ─────────────────────────────────────
    stale_planning = await db.execute(
        text("""
            SELECT id, updated_at
            FROM jobs
            WHERE status = 'planning'
              AND updated_at < NOW() - INTERVAL '60 minutes'
        """)
    )
    planning_rows = stale_planning.fetchall()

    for row in planning_rows:
        job_id = row[0]
        updated_at = row[1]
        age_minutes = round((now - updated_at.replace(tzinfo=timezone.utc)).total_seconds() / 60, 1)

        await db.execute(
            text("""
                UPDATE jobs
                SET status = 'cancelled',
                    updated_at = NOW()
                WHERE id = :jid
            """),
            {"jid": str(job_id)},
        )
        logger.info(
            'event="stale_job_cleaned" job_id=%s old_status=planning new_status=cancelled age_minutes=%s',
            job_id, age_minutes,
        )

    await db.commit()

    return {
        "cleaned": {
            "running_to_failed": len(running_rows),
            "planning_to_cancelled": len(planning_rows),
        },
        "timestamp": now.isoformat(),
    }


# ── Endpoint ─────────────────────────────────────────────────────────

@router.post("/cleanup")
async def cleanup_stale_jobs(db=Depends(get_db), _auth=Depends(verify_api_key)):
    """Identify and resolve orphaned/stale jobs. Requires API key."""
    return await _do_cleanup(db)


# ── Startup hook ─────────────────────────────────────────────────────

async def run_cleanup_if_enabled():
    """
    Call from app startup. Only runs if CLEANUP_ON_STARTUP=true.
    Requires an independent DB session (not from request context).
    """
    if os.getenv("CLEANUP_ON_STARTUP", "").lower() != "true":
        return

    logger.info('event="startup_cleanup_begin"')
    try:
        # Get a session outside of request context.
        # Adjust this to match your session factory pattern.
        async for db in get_db():
            result = await _do_cleanup(db)
            logger.info(
                'event="startup_cleanup_complete" running_to_failed=%s planning_to_cancelled=%s',
                result["cleaned"]["running_to_failed"],
                result["cleaned"]["planning_to_cancelled"],
            )
            break
    except Exception as exc:
        logger.error('event="startup_cleanup_failed" error=%s', exc)
