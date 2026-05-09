"""Sprint X.20 — system-wide observability rollups.

Three read-only endpoints over existing telemetry tables:

  GET   /observability/llm           — llm_call_logs aggregated by (provider, model)
  GET   /observability/errors        — recent error_logs filtered by resolved flag
  GET   /observability/jobs          — recent jobs joined with their LLM totals
  PATCH /observability/errors/{id}   — mark an error_log row resolved (M4)

All read endpoints fail-open: a missing telemetry table or transient
DB error returns the zero/empty shape, never 500. Mounted in
app/main.py via ``app.include_router(observability_router)``;
inherits the global ``Depends(require_api_key)``.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.modules import observability_rollups
from app.schemas import ErrorLogResolveInput, ErrorLogResolveResponse

router = APIRouter(tags=["Observability"])


@router.get("/observability/llm")
async def llm_rollup_endpoint(
    window_minutes: int = Query(60, ge=1, le=10080,
        description="Aggregation window in minutes (max 7 days)."),
    provider: str | None = Query(None,
        description="Filter to one provider (e.g. 'openai', 'ollama')."),
    model: str | None = Query(None,
        description="Filter to one model (exact match, e.g. 'gpt-4o')."),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """System-wide LLM cost + latency rollup.

    Aggregates ``llm_call_logs`` over the last ``window_minutes`` minutes,
    grouped by ``(provider, model)`` and sorted by total cost DESC.
    Includes call count, success/failure split, token totals, and
    p50/p95/p99 latency per group.

    Complements ``GET /jobs/{id}/costs`` (per-job) — this is the
    "what's the system spending right now" view.
    """
    return await observability_rollups.llm_rollup(
        window_minutes=window_minutes,
        provider=provider,
        model=model,
        db=db,
    )


@router.get("/observability/errors")
async def recent_errors_endpoint(
    resolved: bool | None = Query(None,
        description="Filter by resolved flag. Omit for all; pass false for an oncall view."),
    since_minutes: int | None = Query(None, ge=1, le=10080,
        description="Only errors created within the last N minutes."),
    limit: int = Query(50, ge=1, le=500,
        description="Max rows returned (default 50)."),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Recent error_logs rows for an oncall view.

    Pass ``resolved=false`` for "what's still broken." ``since_minutes``
    bounds how far back to look — useful for "errors in the last hour"
    triage. Sorted newest first.
    """
    return await observability_rollups.recent_errors(
        resolved=resolved,
        since_minutes=since_minutes,
        limit=limit,
        db=db,
    )


@router.get("/observability/jobs")
async def recent_jobs_endpoint(
    window_minutes: int = Query(1440, ge=1, le=10080,
        description="Job creation window in minutes (default 24h)."),
    limit: int = Query(25, ge=1, le=200,
        description="Max rows returned (default 25)."),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Recent jobs with their LLM cost + latency totals.

    Lists jobs created within the window, sorted by total cost DESC
    (then created_at DESC). Each row carries the job's status and its
    aggregated cost/tokens/latency from ``llm_call_logs``. Jobs with
    zero logged calls (planning-only, or pre-J.3.a) appear with zeros
    rather than being omitted.

    Useful for "what's been expensive in the last hour/day" without
    paging through ``/jobs`` and calling ``/jobs/{id}/costs`` per row.
    """
    return await observability_rollups.recent_jobs_costs(
        window_minutes=window_minutes,
        limit=limit,
        db=db,
    )


@router.patch(
    "/observability/errors/{error_id}",
    response_model=ErrorLogResolveResponse,
)
async def resolve_error_endpoint(
    error_id: str,
    body: ErrorLogResolveInput,
    db: AsyncSession = Depends(get_db),
) -> ErrorLogResolveResponse:
    """Audit M4 — mark an error_log row resolved (or un-resolved).

    Body ``{"resolved": true, "resolution": "..."}`` flips the row's
    ``resolved`` flag, stores the optional triage note, and stamps
    ``resolved_at = NOW()``. Body ``{"resolved": false}`` un-resolves
    the row and clears ``resolved_at``; the resolution note is
    overwritten to whatever the caller passes (None to clear).

    Without this endpoint there was no API mechanism for clearing
    ``error_logs`` rows, so the X.26 ``alert_unresolved_errors_threshold``
    threshold went off as soon as the first ever error was recorded
    and never cleared.

    422 on bad UUID; 404 if the row doesn't exist.
    """
    try:
        UUID(error_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=422, detail="error_id must be a valid UUID")

    r = await db.execute(text("""
        UPDATE error_logs
           SET resolved = :resolved,
               resolution = :resolution,
               resolved_at = CASE WHEN :resolved THEN NOW() ELSE NULL END
         WHERE id = :id
        RETURNING id, resolved, resolution, resolved_at
    """), {
        "id": error_id,
        "resolved": body.resolved,
        "resolution": body.resolution,
    })
    row = r.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"error_log not found: {error_id}")
    await db.commit()
    return ErrorLogResolveResponse(
        error_id=str(row.id),
        resolved=row.resolved,
        resolution=row.resolution,
        resolved_at=row.resolved_at.isoformat() if row.resolved_at else None,
    )
