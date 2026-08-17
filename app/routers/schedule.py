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

from app.authz import (
    Principal,
    assert_visible,
    get_principal,
    owner_filter,
)
from app.database import get_db
from app.schemas import ScheduleCreate, ScheduleResponse
from app.utils.model_validation import _require_valid_models

router = APIRouter()


@router.post("/schedule", response_model=ScheduleResponse)
async def create_schedule(
    body: ScheduleCreate,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """Create a recurring research schedule."""
    from apscheduler.triggers.cron import CronTrigger
    from app.scheduler import add_schedule, remove_schedule

    try:
        CronTrigger.from_crontab(body.cron_expression, timezone=body.timezone)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"invalid cron expression or timezone: {exc}")

    await _require_valid_models(body.model_overrides)

    # §17.810 — stamp the creating principal; the owner is propagated to every
    # research session this schedule later spawns (app/scheduler.py).
    result = await db.execute(text("""
        INSERT INTO scheduled_jobs (topic, depth, cron_expression, timezone, domain, enabled, owner)
        VALUES (:topic, :depth, :cron, :tz, :domain, TRUE, :owner)
        RETURNING id, topic, depth, cron_expression, timezone, domain, enabled,
                  last_run_at, last_status, last_job_id, next_run_at,
                  run_count, failure_count, created_at
    """), {"topic": body.topic, "depth": body.depth, "cron": body.cron_expression,
           "tz": body.timezone, "domain": body.domain, "owner": principal.identity})
    row = result.mappings().first()

    # APScheduler registration + next_run_at UPDATE both run in this same
    # session so the UPDATE can see the still-uncommitted INSERT.
    #
    # §17.418 — the APScheduler jobstore commits on its OWN engine, so it is
    # NOT in this transaction. If add_schedule itself raises, its internal
    # except already removed the job. But if add_schedule SUCCEEDS and our
    # db.commit() then fails, the job is committed to apscheduler_jobs while
    # the scheduled_jobs INSERT rolls back — an orphan. So on commit failure
    # we explicitly remove_schedule() to drop it. (The startup
    # _reconcile_orphans pass is the backstop for crashes that skip this.)
    try:
        next_run = await add_schedule(
            db, row["id"], row["topic"], row["depth"],
            row["cron_expression"], row["timezone"], row["domain"],
        )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        try:
            await remove_schedule(row["id"])
        except Exception:
            pass  # reconciliation backstop will prune it on next restart
        raise HTTPException(status_code=502, detail=f"scheduler registration failed: {exc}")
    response = dict(row)
    response["next_run_at"] = next_run
    return response


@router.get("/schedule")
async def list_schedules(
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=422, detail="limit must be 1..200")
    if offset < 0:
        raise HTTPException(status_code=422, detail="offset must be >= 0")
    # §17.810 — non-admin sees only their own schedules; admin sees all.
    owner_clause, owner_params = owner_filter(principal, column="owner")
    where_sql = f"WHERE {owner_clause.removeprefix(' AND ')}" if owner_clause else ""
    total = (await db.execute(
        text(f"SELECT COUNT(*) FROM scheduled_jobs {where_sql}"), owner_params
    )).scalar() or 0
    rows = (await db.execute(text(f"""
        SELECT id, topic, depth, cron_expression, timezone, domain, enabled,
               last_run_at, last_status, last_job_id, next_run_at,
               run_count, failure_count, created_at
        FROM scheduled_jobs
        {where_sql}
        ORDER BY created_at DESC
        LIMIT :limit OFFSET :offset
    """), {"limit": limit, "offset": offset, **owner_params})).mappings().all()
    return {
        "schedules": [dict(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.delete("/schedule/{schedule_id}")
async def delete_schedule(
    schedule_id: int,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    from app.scheduler import delete_schedule as _scheduler_delete

    # §17.810 — only the owner (or admin) may delete a schedule. Pre-check so a
    # non-owner gets the same 404 as a missing schedule (no existence leak).
    await assert_visible(
        db, principal, schedule_id,
        table="scheduled_jobs", detail="schedule not found",
    )
    deleted = await _scheduler_delete(db, schedule_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="schedule not found")
    # §17.418 — symmetry with the add path. delete_schedule already removed the
    # APScheduler job (separate jobstore engine); on a commit failure the
    # scheduled_jobs DELETE rolls back so the row survives, and startup
    # _rehydrate re-adds its job on the next restart. Surface a clean 502
    # instead of a raw 500.
    try:
        await db.commit()
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=502, detail=f"schedule delete commit failed: {exc}")
    return {"deleted": schedule_id}
