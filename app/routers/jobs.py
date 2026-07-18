"""§17.174 — jobs lifecycle endpoints (list/get/delete/rename/costs/synthesis/resume/cancel/cleanup).

Extracted from ``app/main.py`` as part of the §17.174 router refactor.
Endpoint paths, function names, tags, and response_models are
preserved verbatim so the committed ``docs/openapi.json`` snapshot
stays byte-identical post-refactor.

Routes:
  POST   /jobs/cleanup                  — cleanup_stale_jobs (ops)
  POST   /jobs/{job_id}/resume          — resume_job_endpoint (Management, SSE)
  POST   /jobs/{job_id}/cancel          — cancel_job_endpoint (Management) [§17.322]
  GET    /jobs                          — list_jobs (Management)
  DELETE /jobs/{job_id}                 — delete_job (Management)
  PATCH  /jobs/{job_id}                 — rename_job (Management)
  GET    /jobs/{job_id}/costs           — get_job_costs_endpoint (Management)
  PATCH  /jobs/{job_id}/synthesis       — set_job_synthesis_override (Management)
"""
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from app.database import get_db
from app.modules.cleanup import reap_stale_jobs
from app.modules.execution_agent import execute_all_nodes
from app.modules.execution_handler import cancel_active_job, resume_cancelled_job
from app.schemas import (
    CancelJobResult,
    DeleteResponse,
    JOB_STATUSES,
    JobCostsBreakdownItem,
    JobCostsResponse,
    JobListResponse,
    JobRenameInput,
    JobSummary,
    JobSynthesisOverrideInput,
    JobSynthesisOverrideResponse,
    ResumeJobInput,
)
from app.utils.model_validation import _require_valid_models

router = APIRouter()


@router.post("/jobs/cleanup", tags=["ops"])
async def cleanup_stale_jobs(db: AsyncSession = Depends(get_db)):
    """Find and resolve stale/orphaned jobs. Requires API key (global auth)."""
    result = await reap_stale_jobs(db)
    return {
        "cleaned": result,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/jobs/{job_id}/resume", tags=["Management"])
async def resume_job_endpoint(
    job_id: str,
    body: ResumeJobInput,
    db: AsyncSession = Depends(get_db),
):
    """Resume a cancelled job and stream its execution.

    Atomically flips the job from ``cancelled`` back to ``executing`` and
    re-fires ``/execute/all``-equivalent execution. Replaces the manual
    SQL-then-curl recipe in debugging.md. ``execute_all_nodes`` is
    idempotent over completed nodes — execution picks up from the last
    pending node, with done-node outputs serving as upstream context.

    Status codes:
      - 200 + SSE stream on successful resume
      - 404 if no job with that ID exists
      - 409 if the job exists but isn't in ``cancelled`` (current status
        returned in detail for client-side dispatch)
      - 400 on malformed UUID
    """
    try:
        parsed_id = UUID(job_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid job_id format")

    await _require_valid_models(body.model_overrides)
    outcome = await resume_cancelled_job(parsed_id, db)

    if outcome["outcome"] == "not_found":
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    if outcome["outcome"] == "wrong_status":
        raise HTTPException(
            status_code=409,
            detail={
                "error": "job not resumable",
                "current_status": outcome["current_status"],
                "expected_status": "cancelled",
            },
        )
    # outcome == "resumed" — start streaming.
    return StreamingResponse(
        execute_all_nodes(job_id, model_overrides=body.model_overrides),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no"},
    )


@router.post(
    "/jobs/{job_id}/cancel",
    response_model=CancelJobResult,
    tags=["Management"],
)
async def cancel_job_endpoint(
    job_id: str,
    db: AsyncSession = Depends(get_db),
):
    """§17.322 — operator-driven cancel for a non-terminal job.

    Symmetric to ``POST /jobs/{job_id}/resume`` (§17.130): resume is
    cancelled→executing; cancel is active→cancelled. Replaces the
    SQL-only drain mechanism §17.321 had to use for the 4 stuck
    ``awaiting_confirmation`` jobs.

    Status codes:
      - 200 + ``CancelJobResult{cancelled=True, was_already_cancelled=False}``
        on a successful active→cancelled flip (any of pending /
        refining / awaiting_confirmation / researching / planning /
        executing / running / blocked / assisted_*)
      - 200 + ``CancelJobResult{cancelled=True, was_already_cancelled=True}``
        when the job was already cancelled; idempotent OK
      - 404 if no job with that ID exists
      - 409 if the job is in a terminal non-cancellable state
        (``completed`` or ``failed``); ``current_status`` in detail
      - 422 on malformed UUID

    No request body. Cancellation reason is operator-context; if a
    structured reason field becomes useful (audit trail, scheduled
    cancels) a v2 schema can accept it. Pre-v1 the
    ``ideation_workflow._cancel_job`` helper writes
    ``error_summary='client_disconnect'`` for SSE-disconnect paths and
    is unaffected by this endpoint.
    """
    try:
        parsed_id = UUID(job_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=422, detail="job_id must be a valid UUID")

    outcome = await cancel_active_job(parsed_id, db)

    if outcome["outcome"] == "not_found":
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
    if outcome["outcome"] == "wrong_status":
        raise HTTPException(
            status_code=409,
            detail={
                "error": "job not cancellable",
                "current_status": outcome["current_status"],
                "reason": (
                    "Terminal jobs (completed / failed) cannot be cancelled. "
                    "Use DELETE /jobs/{id} to hard-delete instead."
                ),
            },
        )
    if outcome["outcome"] == "already_cancelled":
        return CancelJobResult(
            id=str(parsed_id),
            cancelled=True,
            was_already_cancelled=True,
            status_before=outcome["status_before"],
            status_after=outcome["status_after"],
        )
    # outcome == "cancelled" — active→cancelled flip succeeded.
    return CancelJobResult(
        id=str(parsed_id),
        cancelled=True,
        was_already_cancelled=False,
        status_before=outcome["status_before"],
        status_after=outcome["status_after"],
    )


@router.get("/jobs", response_model=JobListResponse, tags=["Management"])
async def list_jobs(
    status: str | None = None,
    q: str | None = None,
    synthesized: bool | None = None,
    limit: int = 25,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """Paginated job list with optional status filter and title search.

    Sprint X.9 — ``synthesized`` filter complements the X.6 per-job opt-in:
    ``?synthesized=true`` lists only jobs whose ``compiled_output`` was
    LLM-synthesized (W.7 narrative pass); ``?synthesized=false`` lists
    everything else (heuristic compile, unsynthesized, or not-yet-compiled).
    Omit the param to see all jobs.
    """
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit must be 1..100")
    if offset < 0:
        raise HTTPException(status_code=422, detail="offset must be >= 0")

    where_clauses = []
    params: dict = {}
    if status:
        if status not in JOB_STATUSES:
            raise HTTPException(status_code=422, detail=f"invalid status: {status}")
        where_clauses.append("j.status = :status")
        params["status"] = status
    if q:
        where_clauses.append("j.title ILIKE :q")
        params["q"] = f"%{q.strip()}%"
    if synthesized is not None:
        where_clauses.append("j.compiled_output_synthesized = :synthesized")
        params["synthesized"] = synthesized

    # SAFE: where_clauses contain only bind-parameter placeholders (:status, :q,
    # :synthesized); all user values flow through `params` dict. Do not
    # interpolate user input into where_clauses directly without enum/whitelist
    # validation first.
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    total_row = await db.execute(text(f"SELECT COUNT(*) FROM jobs j {where_sql}"), params)
    total = total_row.scalar() or 0

    params["limit"] = limit
    params["offset"] = offset
    rows = await db.execute(text(f"""
        SELECT j.id, j.title, j.status, j.created_at, j.updated_at,
               j.completed_at, j.parent_job_id, j.component_index,
               COALESCE(n.cnt, 0) AS node_count
        FROM jobs j
        LEFT JOIN (SELECT job_id, COUNT(*) AS cnt FROM dag_nodes GROUP BY job_id) n
          ON n.job_id = j.id
        {where_sql}
        ORDER BY j.updated_at DESC
        LIMIT :limit OFFSET :offset
    """), params)
    jobs = [
        JobSummary(
            id=str(r.id),
            title=r.title or "",
            status=r.status,
            node_count=r.node_count,
            created_at=r.created_at.isoformat(),
            updated_at=r.updated_at.isoformat(),
            completed_at=r.completed_at.isoformat() if r.completed_at else None,
            # §17.617 (audit #19) — populate the §17.525 decomposition fields so
            # clients can distinguish umbrella/component jobs (were always null).
            parent_job_id=str(r.parent_job_id) if r.parent_job_id else None,
            component_index=r.component_index,
        )
        for r in rows.fetchall()
    ]
    return JobListResponse(jobs=jobs, total=total, limit=limit, offset=offset)


@router.delete("/jobs/{job_id}", response_model=DeleteResponse, tags=["Management"])
async def delete_job(job_id: str, db: AsyncSession = Depends(get_db)):
    """Hard-delete a job. Cascade removes dag_nodes / execution_logs / artifacts /
    error_logs (FK ON DELETE CASCADE). llm_call_logs rows are unaffected
    (no FK; off-job calls live there too)."""
    try:
        UUID(job_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=422, detail="job_id must be a valid UUID")

    r = await db.execute(text("DELETE FROM jobs WHERE id = :id RETURNING id"), {"id": job_id})
    if r.fetchone() is None:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
    await db.commit()
    return DeleteResponse(deleted=True, id=job_id)


@router.patch("/jobs/{job_id}", response_model=JobSummary, tags=["Management"])
async def rename_job(job_id: str, body: JobRenameInput, db: AsyncSession = Depends(get_db)):
    """Rename a job (set title)."""
    try:
        UUID(job_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=422, detail="job_id must be a valid UUID")

    r = await db.execute(text("""
        UPDATE jobs SET title = :title, updated_at = NOW()
        WHERE id = :id
        RETURNING id, title, status, created_at, updated_at, completed_at,
                  parent_job_id, component_index,
                  (SELECT COUNT(*) FROM dag_nodes WHERE job_id = :id) AS node_count
    """), {"id": job_id, "title": body.title})
    row = r.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
    await db.commit()
    return JobSummary(
        id=str(row.id), title=row.title, status=row.status,
        node_count=row.node_count or 0,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
        completed_at=row.completed_at.isoformat() if row.completed_at else None,
        # §17.617 (audit #19) — carry the decomposition fields on the rename response too.
        parent_job_id=str(row.parent_job_id) if row.parent_job_id else None,
        component_index=row.component_index,
    )


@router.get(
    "/jobs/{job_id}/costs",
    response_model=JobCostsResponse,
    tags=["Management"],
)
async def get_job_costs_endpoint(
    job_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Sprint J.3.b — aggregate cost + latency for a job.

    Returns total USD spent (computed at insert time, immutable),
    total prompt/completion tokens, total LLM latency, the count of
    LLM calls logged for this job, and a per-(provider, model)
    breakdown sorted by descending cost. Job_ids with no logged
    calls return the zero shape with an empty breakdown — fail-open
    matches the rest of the cost-tracking surface.
    """
    try:
        UUID(job_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=422, detail="job_id must be a valid UUID")

    from app.modules.cost_rollup import get_job_costs
    payload = await get_job_costs(job_id, db)
    return JobCostsResponse(
        job_id=payload["job_id"],
        total_cost_usd=payload["total_cost_usd"],
        total_prompt_tokens=payload["total_prompt_tokens"],
        total_completion_tokens=payload["total_completion_tokens"],
        total_latency_ms=payload["total_latency_ms"],
        call_count=payload["call_count"],
        by_provider=[JobCostsBreakdownItem(**row) for row in payload["by_provider"]],
    )


@router.patch(
    "/jobs/{job_id}/synthesis",
    response_model=JobSynthesisOverrideResponse,
    tags=["Management"],
)
async def set_job_synthesis_override(
    job_id: str,
    body: JobSynthesisOverrideInput,
    db: AsyncSession = Depends(get_db),
):
    """Sprint X.6 — set the per-job opt-in for the W.7 LLM synthesis pass.

    Body ``{"override": true}`` forces synthesis on for this job;
    ``{"override": false}`` forces it off; ``{"override": null}`` clears
    the override so the job inherits ``settings.compile_synthesis_enabled``.

    The override is read by ``execution_compile._resolve_synthesis_enabled``
    on the next compile pass — set it before ``/execute/all`` (or before
    a final-node retry) for it to take effect on the resulting deliverable.
    """
    try:
        UUID(job_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=422, detail="job_id must be a valid UUID")

    r = await db.execute(text("""
        UPDATE jobs
           SET compile_synthesis_override = :override,
               updated_at = NOW()
         WHERE id = :id
        RETURNING id, compile_synthesis_override
    """), {"id": job_id, "override": body.override})
    row = r.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
    await db.commit()
    return JobSynthesisOverrideResponse(
        job_id=str(row.id),
        override=row.compile_synthesis_override,
    )
