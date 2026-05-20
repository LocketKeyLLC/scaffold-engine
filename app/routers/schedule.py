"""§17.174 — scheduled research jobs endpoints.

Extracted from ``app/main.py`` as part of the §17.174 router refactor.
Endpoint paths, function names, tags, and response_models are
preserved verbatim so the committed ``docs/openapi.json`` snapshot
stays byte-identical post-refactor.

Routes:
  POST   /schedule                  — create_schedule
  GET    /schedule                  — list_schedules
  DELETE /schedule/{schedule_id}    — delete_schedule
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas import ScheduleCreate, ScheduleResponse
from app.utils.model_validation import _require_valid_models

router = APIRouter()


@router.post("/schedule", response_model=ScheduleResponse)
async def create_schedule(body: ScheduleCreate, db: AsyncSession = Depends(get_db)):
    """Create a recurring research schedule."""
    from apscheduler.triggers.cron import CronTrigger
    from app.scheduler import add_schedule

    try:
        CronTrigger.from_crontab(body.cron_expression, timezone=body.timezone)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"invalid cron expression or timezone: {exc}")

    await _require_valid_models(body.model_overrides)

    result = await db.execute(text("""
        INSERT INTO scheduled_jobs (topic, depth, cron_expression, timezone, enabled)
        VALUES (:topic, :depth, :cron, :tz, TRUE)
        RETURNING id, topic, depth, cron_expression, timezone, enabled,
                  last_run_at, last_status, last_job_id, next_run_at,
                  run_count, failure_count, created_at
    """), {"topic": body.topic, "depth": body.depth, "cron": body.cron_expression, "tz": body.timezone})
    row = result.mappings().first()

    # APScheduler registration + next_run_at UPDATE both run in this same
    # session so the UPDATE can see the still-uncommitted INSERT. On any
    # failure, db.rollback() unwinds the INSERT and add_schedule() has
    # already removed its APScheduler entry, leaving system state aligned.
    try:
        next_run = await add_schedule(
            db, row["id"], row["topic"], row["depth"],
            row["cron_expression"], row["timezone"],
        )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=502, detail=f"scheduler registration failed: {exc}")
    response = dict(row)
    response["next_run_at"] = next_run
    return response


@router.get("/schedule")
async def list_schedules(
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=422, detail="limit must be 1..200")
    if offset < 0:
        raise HTTPException(status_code=422, detail="offset must be >= 0")
    total = (await db.execute(
        text("SELECT COUNT(*) FROM scheduled_jobs")
    )).scalar() or 0
    rows = (await db.execute(text("""
        SELECT id, topic, depth, cron_expression, timezone, enabled,
               last_run_at, last_status, last_job_id, next_run_at,
               run_count, failure_count, created_at
        FROM scheduled_jobs
        ORDER BY created_at DESC
        LIMIT :limit OFFSET :offset
    """), {"limit": limit, "offset": offset})).mappings().all()
    return {
        "schedules": [dict(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.delete("/schedule/{schedule_id}")
async def delete_schedule(schedule_id: int, db: AsyncSession = Depends(get_db)):
    from app.scheduler import delete_schedule as _scheduler_delete

    deleted = await _scheduler_delete(db, schedule_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="schedule not found")
    await db.commit()
    return {"deleted": schedule_id}
