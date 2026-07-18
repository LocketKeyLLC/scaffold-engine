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

§17.284 — every return carries ``data_source`` (``"ok"`` | ``"error"``)
so callers can distinguish a real empty rollup (no data in window) from
a fail-open fallback (query raised). Mirrors the same flag added to
``cost_rollup`` in §17.284.
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
    data_source = "ok"
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
        data_source = "error"

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
        "data_source": data_source,
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
    data_source = "ok"
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
        data_source = "error"

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
        "data_source": data_source,
    }


# ── 3. Recent jobs cost rollup ───────────────────────────────────────
#
# Joins ``jobs`` with a per-job aggregate of ``llm_call_logs``. Filtered
# by job ``created_at`` (the wall-clock window) since llm_call_logs may
# trail or lead a job's lifecycle. ``LEFT JOIN`` so jobs with zero
# logged calls (planning-only, or pre-J.3.a) still appear with zeros.

#
# §17.611 (audit #10) — join-then-aggregate. The prior form aggregated the
# ENTIRE, unbounded, never-pruned llm_call_logs table in a derived subquery
# before hash-joining to the small windowed jobs set (the window predicate
# referenced jobs, so Postgres could not push it into the nullable-side
# subquery). A "last hour" query therefore scanned months of call history and
# degraded linearly with total LLM volume. Filtering jobs by the window FIRST
# and probing llm_call_logs via idx_llm_call_logs_job_id keeps cost O(window),
# matching the sibling llm_rollup / cost_rollup / _NODE_QUALITY_SQL readers.
_JOBS_COSTS_SQL = """
    SELECT
        j.id            AS job_id,
        j.status        AS job_status,
        j.created_at    AS job_created_at,
        COUNT(c.job_id)                         AS calls,
        COALESCE(SUM(c.cost_usd), 0)            AS cost_usd,
        COALESCE(SUM(c.prompt_tokens), 0)       AS prompt_tokens,
        COALESCE(SUM(c.completion_tokens), 0)   AS completion_tokens,
        COALESCE(SUM(c.latency_ms), 0)          AS latency_ms
    FROM jobs j
    LEFT JOIN llm_call_logs c ON c.job_id = j.id
    WHERE j.created_at >= NOW() - make_interval(mins => :window_minutes)
    GROUP BY j.id
    ORDER BY cost_usd DESC NULLS LAST, j.created_at DESC
    LIMIT :limit
"""


# ── 4. Quality rollup (§17.573) ──────────────────────────────────────
#
# Aggregates already-recorded execution-quality signals so operators can
# see WHICH tool/node-type is failing, low-confidence, or retry-heavy, and
# the grounding-score distribution across recent deliverables — the data
# the model A/B + grounding work tunes against. Windowed by the parent
# job's created_at (same wall-clock basis as recent_jobs_costs). Fail-open.

_NODE_QUALITY_SQL = """
    SELECT
        COALESCE(n.tool, '')      AS tool,
        COALESCE(n.node_type, '') AS node_type,
        COUNT(*)                                       AS total,
        COUNT(*) FILTER (WHERE n.status = 'done')      AS done,
        COUNT(*) FILTER (WHERE n.status = 'failed')    AS failed,
        COUNT(*) FILTER (WHERE n.status = 'skipped')   AS skipped,
        COALESCE(AVG(n.confidence)
                 FILTER (WHERE n.confidence IS NOT NULL), 0) AS avg_confidence,
        COALESCE(AVG(n.retry_count), 0)                AS avg_retry_count
    FROM dag_nodes n
    JOIN jobs j ON j.id = n.job_id
    WHERE j.created_at >= NOW() - make_interval(mins => :window_minutes)
    GROUP BY n.tool, n.node_type
    ORDER BY total DESC
"""

_GROUNDING_DIST_SQL = """
    SELECT
        COUNT(*)                                                       AS jobs_scored,
        COALESCE(AVG((metadata->'grounding'->>'score')::float), 0)     AS avg_score,
        COALESCE(MIN((metadata->'grounding'->>'score')::float), 0)     AS min_score,
        COUNT(*) FILTER (
            WHERE COALESCE((metadata->'grounding'->>'corrected')::boolean, FALSE)
        )                                                              AS corrected,
        COUNT(*) FILTER (
            WHERE (metadata->'grounding'->>'score')::float < :min_score
        )                                                              AS below_threshold
    FROM jobs
    WHERE metadata ? 'grounding'
      AND created_at >= NOW() - make_interval(mins => :window_minutes)
"""


async def quality_rollup(
    *,
    window_minutes: int,
    grounding_threshold: float = 0.7,
    db,
) -> dict[str, Any]:
    """Execution-quality rollup over recent jobs' nodes + grounding scores.

    ``by_node_type``: per (tool, node_type) — total/done/failed/skipped,
    pass_rate (done / (done+failed)), avg confidence, avg retry count.
    ``grounding``: distribution of ``jobs.metadata.grounding.score`` —
    count scored, avg/min, # auto-corrected, # below threshold.
    Fail-open: zero/empty shapes + ``data_source='error'`` on any DB error.
    """
    data_source = "ok"
    node_records: list = []
    grounding_row: dict | None = None
    try:
        rows = await db.execute(
            text(_NODE_QUALITY_SQL), {"window_minutes": window_minutes},
        )
        node_records = rows.mappings().all()
        grow = await db.execute(
            text(_GROUNDING_DIST_SQL),
            {"window_minutes": window_minutes, "min_score": grounding_threshold},
        )
        grounding_row = grow.mappings().first()
    except Exception as exc:
        logger.debug(
            "quality_rollup_failed: window=%dm err=%s (returning empty)",
            window_minutes, exc,
        )
        data_source = "error"

    by_node_type = []
    for r in node_records:
        done, failed = int(r["done"] or 0), int(r["failed"] or 0)
        decided = done + failed
        by_node_type.append({
            "tool": r["tool"],
            "node_type": r["node_type"],
            "total": int(r["total"] or 0),
            "done": done,
            "failed": failed,
            "skipped": int(r["skipped"] or 0),
            "pass_rate": round(done / decided, 3) if decided else None,
            "avg_confidence": round(float(r["avg_confidence"] or 0.0), 3),
            "avg_retry_count": round(float(r["avg_retry_count"] or 0.0), 3),
        })

    g = grounding_row or {}
    grounding = {
        "jobs_scored": int(g.get("jobs_scored", 0) or 0),
        "avg_score": round(float(g.get("avg_score", 0.0) or 0.0), 3),
        "min_score": round(float(g.get("min_score", 0.0) or 0.0), 3),
        "corrected": int(g.get("corrected", 0) or 0),
        "below_threshold": int(g.get("below_threshold", 0) or 0),
        "threshold": grounding_threshold,
    }
    return {
        "window_minutes": window_minutes,
        "by_node_type": by_node_type,
        "grounding": grounding,
        "data_source": data_source,
    }


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
    data_source = "ok"
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
        data_source = "error"

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
        "data_source": data_source,
    }
