"""System-wide observability rollups (Sprint X.20).

Three readers over the existing telemetry tables:

  - ``llm_rollup(window_minutes, ...)`` — aggregate `llm_call_logs`
    by ``(provider, model)`` with totals + p50/p95/p99 latency.
    System-wide complement to ``cost_rollup.get_job_costs`` (per-job).

  - ``recent_errors(...)`` — recent ``error_logs`` rows for an oncall
    view. Filters: ``resolved`` flag, optional ``since_minutes``.

  - ``recent_jobs_costs(window_minutes, limit)`` — recent jobs with
    their cost/latency totals joined from ``llm_call_logs``. Useful
    for "what's been expensive lately" without paging through /jobs.

Same fail-open contract as ``cost_rollup``: a missing telemetry
table or transient DB error returns the zero/empty shape, never 500.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

logger = logging.getLogger("scaffold.observability")


# ── 1. LLM rollup ────────────────────────────────────────────────────

_LLM_ROLLUP_SQL = """
    SELECT
        provider,
        model,
        COUNT(*)                                          AS calls,
        COUNT(*) FILTER (WHERE success = TRUE)            AS successes,
        COUNT(*) FILTER (WHERE success = FALSE)           AS failures,
        COALESCE(SUM(cost_usd), 0)                        AS cost_usd,
        COALESCE(SUM(prompt_tokens), 0)                   AS prompt_tokens,
        COALESCE(SUM(completion_tokens), 0)               AS completion_tokens,
        COALESCE(SUM(latency_ms), 0)                      AS latency_ms_sum,
        COALESCE(percentile_cont(0.5)
                 WITHIN GROUP (ORDER BY latency_ms), 0)   AS latency_ms_p50,
        COALESCE(percentile_cont(0.95)
                 WITHIN GROUP (ORDER BY latency_ms), 0)   AS latency_ms_p95,
        COALESCE(percentile_cont(0.99)
                 WITHIN GROUP (ORDER BY latency_ms), 0)   AS latency_ms_p99
    FROM llm_call_logs
    WHERE created_at >= NOW() - make_interval(mins => :window_minutes)
      AND (CAST(:provider_filter AS TEXT) IS NULL OR provider = CAST(:provider_filter AS TEXT))
      AND (CAST(:model_filter    AS TEXT) IS NULL OR model    = CAST(:model_filter    AS TEXT))
    GROUP BY provider, model
    ORDER BY cost_usd DESC, calls DESC
"""


async def llm_rollup(
    *,
    window_minutes: int,
    provider: str | None = None,
    model: str | None = None,
    db,
) -> dict[str, Any]:
    """Aggregate llm_call_logs by (provider, model) over a time window.

    Returns a dict with ``window_minutes``, ``total_calls``,
    ``total_cost_usd``, and ``by_model`` list. Fail-open: empty list
    + zero totals on any DB error.
    """
    try:
        rows = await db.execute(
            text(_LLM_ROLLUP_SQL),
            {
                "window_minutes": window_minutes,
                "provider_filter": provider,
                "model_filter": model,
            },
        )
        records = rows.mappings().all()
    except Exception as exc:
        logger.debug(
            "llm_rollup_failed: window=%dm provider=%s model=%s err=%s "
            "(returning empty)", window_minutes, provider, model, exc,
        )
        records = []

    by_model = [
        {
            "provider": r["provider"],
            "model": r["model"],
            "calls": int(r["calls"] or 0),
            "successes": int(r["successes"] or 0),
            "failures": int(r["failures"] or 0),
            "cost_usd": float(r["cost_usd"] or 0.0),
            "prompt_tokens": int(r["prompt_tokens"] or 0),
            "completion_tokens": int(r["completion_tokens"] or 0),
            "latency_ms_sum": int(r["latency_ms_sum"] or 0),
            "latency_ms_p50": int(r["latency_ms_p50"] or 0),
            "latency_ms_p95": int(r["latency_ms_p95"] or 0),
            "latency_ms_p99": int(r["latency_ms_p99"] or 0),
        }
        for r in records
    ]
    return {
        "window_minutes": window_minutes,
        "total_calls": sum(b["calls"] for b in by_model),
        "total_cost_usd": round(sum(b["cost_usd"] for b in by_model), 6),
        "by_model": by_model,
    }


# ── 2. Recent errors ─────────────────────────────────────────────────

_ERRORS_SQL = """
    SELECT
        id,
        job_id,
        node_id,
        error_type,
        error_message,
        model_used,
        retry_count,
        recovery_action,
        recovery_model,
        resolved,
        resolution,
        created_at,
        resolved_at
    FROM error_logs
    WHERE (CAST(:resolved_filter AS BOOLEAN) IS NULL
           OR resolved = CAST(:resolved_filter AS BOOLEAN))
      AND (CAST(:since_minutes AS INTEGER) IS NULL
           OR created_at >= NOW() - make_interval(mins => CAST(:since_minutes AS INTEGER)))
    ORDER BY created_at DESC
    LIMIT :limit
"""


async def recent_errors(
    *,
    resolved: bool | None = None,
    since_minutes: int | None = None,
    limit: int = 50,
    db,
) -> dict[str, Any]:
    """Recent error_logs rows. Defaults: all (resolved + unresolved),
    no time filter, 50 newest. Caller passes ``resolved=False`` for an
    oncall view of "what's still broken."
    """
    try:
        rows = await db.execute(
            text(_ERRORS_SQL),
            {
                "resolved_filter": resolved,
                "since_minutes": since_minutes,
                "limit": limit,
            },
        )
        records = rows.mappings().all()
    except Exception as exc:
        logger.debug(
            "recent_errors_failed: resolved=%s since=%s limit=%d err=%s "
            "(returning empty)", resolved, since_minutes, limit, exc,
        )
        records = []

    errors = [
        {
            "id": str(r["id"]),
            "job_id": str(r["job_id"]) if r["job_id"] else None,
            "node_id": str(r["node_id"]) if r["node_id"] else None,
            "error_type": r["error_type"],
            "error_message": r["error_message"],
            "model_used": r["model_used"],
            "retry_count": int(r["retry_count"] or 0),
            "recovery_action": r["recovery_action"],
            "recovery_model": r["recovery_model"],
            "resolved": bool(r["resolved"]),
            "resolution": r["resolution"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "resolved_at": r["resolved_at"].isoformat() if r["resolved_at"] else None,
        }
        for r in records
    ]
    return {
        "filters": {
            "resolved": resolved,
            "since_minutes": since_minutes,
            "limit": limit,
        },
        "count": len(errors),
        "errors": errors,
    }


# ── 3. Recent jobs cost rollup ───────────────────────────────────────
#
# Joins ``jobs`` with a per-job aggregate of ``llm_call_logs``. Filtered
# by job ``created_at`` (the wall-clock window) since llm_call_logs may
# trail or lead a job's lifecycle. ``LEFT JOIN`` so jobs with zero
# logged calls (planning-only, or pre-J.3.a) still appear with zeros.

_JOBS_COSTS_SQL = """
    SELECT
        j.id            AS job_id,
        j.status        AS job_status,
        j.created_at    AS job_created_at,
        COALESCE(c.calls, 0)            AS calls,
        COALESCE(c.cost_usd, 0)         AS cost_usd,
        COALESCE(c.prompt_tokens, 0)    AS prompt_tokens,
        COALESCE(c.completion_tokens, 0) AS completion_tokens,
        COALESCE(c.latency_ms, 0)       AS latency_ms
    FROM jobs j
    LEFT JOIN (
        SELECT
            job_id,
            COUNT(*)                            AS calls,
            COALESCE(SUM(cost_usd), 0)          AS cost_usd,
            COALESCE(SUM(prompt_tokens), 0)     AS prompt_tokens,
            COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
            COALESCE(SUM(latency_ms), 0)        AS latency_ms
        FROM llm_call_logs
        WHERE job_id IS NOT NULL
        GROUP BY job_id
    ) c ON c.job_id = j.id
    WHERE j.created_at >= NOW() - make_interval(mins => :window_minutes)
    ORDER BY c.cost_usd DESC NULLS LAST, j.created_at DESC
    LIMIT :limit
"""


async def recent_jobs_costs(
    *,
    window_minutes: int,
    limit: int = 25,
    db,
) -> dict[str, Any]:
    """Recent jobs with their LLM cost/latency totals. Sort by cost DESC
    so the expensive-jobs view is first. Useful for "what's been
    expensive in the last hour/day" without paging through /jobs.
    """
    try:
        rows = await db.execute(
            text(_JOBS_COSTS_SQL),
            {"window_minutes": window_minutes, "limit": limit},
        )
        records = rows.mappings().all()
    except Exception as exc:
        logger.debug(
            "recent_jobs_costs_failed: window=%dm limit=%d err=%s "
            "(returning empty)", window_minutes, limit, exc,
        )
        records = []

    jobs = [
        {
            "job_id": str(r["job_id"]),
            "status": r["job_status"],
            "created_at": r["job_created_at"].isoformat() if r["job_created_at"] else None,
            "calls": int(r["calls"] or 0),
            "cost_usd": float(r["cost_usd"] or 0.0),
            "prompt_tokens": int(r["prompt_tokens"] or 0),
            "completion_tokens": int(r["completion_tokens"] or 0),
            "latency_ms": int(r["latency_ms"] or 0),
        }
        for r in records
    ]
    return {
        "window_minutes": window_minutes,
        "count": len(jobs),
        "total_cost_usd": round(sum(j["cost_usd"] for j in jobs), 6),
        "jobs": jobs,
    }
