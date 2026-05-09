"""Sprint X.26 — push-side X.20 rollup evaluation.

Periodic job (registered by the scheduler) that:

  1. Refreshes the `metrics.py` system-snapshot gauges from the same DB
     queries X.20 already exposes — single source of truth, no drift.
  2. Evaluates three conservative thresholds and emits alerts via
     `app/observability/alerts.emit` when any trip:

        * unresolved errors in the eval window
        * total LLM cost in the eval window
        * any model's p95 latency in the eval window

Thresholds are configurable via env (`alert_*_threshold` settings); the
defaults are the "Conservative" tier. Each alert carries a stable
`dedup_key` so a sustained breach doesn't fan out — the next alert for
the same key fires after `alert_cooldown_seconds` elapses.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

from app.config import settings
from app.database import async_session
from app.modules import observability_rollups
from app.observability import alerts as _alerts
from app.observability import metrics as _metrics

logger = logging.getLogger("scaffold.thresholds")


async def refresh_gauges(db) -> None:
    """Read snapshot counts and push them to Prometheus gauges.

    Cheap: each query is indexed and bounded by status. Fails open — a
    single bad query logs and the others still update.
    """
    # Jobs by status. Cardinality bounded by JOB_STATUSES (~12 values).
    try:
        rows = await db.execute(text(
            "SELECT status, COUNT(*) AS c FROM jobs GROUP BY status"
        ))
        records = rows.mappings().all()
        # Reset previously-seen statuses to 0 so a status that just emptied
        # out doesn't keep its last value forever.
        try:
            _metrics.jobs_by_status.clear()
        except Exception:
            pass
        for r in records:
            _metrics.jobs_by_status.labels(status=r["status"] or "unknown").set(int(r["c"] or 0))
    except Exception as exc:
        logger.debug("refresh_jobs_by_status_failed: err=%s", exc)

    try:
        row = await db.execute(text(
            "SELECT COUNT(*) FROM research_sessions WHERE status = 'running'"
        ))
        _metrics.research_sessions_running.set(int(row.scalar() or 0))
    except Exception as exc:
        logger.debug("refresh_research_sessions_running_failed: err=%s", exc)

    try:
        row = await db.execute(
            text(
                "SELECT COUNT(*) FROM error_logs "
                "WHERE resolved = FALSE "
                "  AND created_at >= NOW() - make_interval(mins => :w)"
            ),
            {"w": settings.alert_eval_window_minutes},
        )
        _metrics.unresolved_errors_window.set(int(row.scalar() or 0))
    except Exception as exc:
        logger.debug("refresh_unresolved_errors_failed: err=%s", exc)


async def evaluate_thresholds(db) -> dict[str, Any]:
    """Evaluate thresholds, fire alerts where breached, return a summary
    dict for tests + structured logging. Never raises."""
    window = settings.alert_eval_window_minutes
    summary: dict[str, Any] = {"window_minutes": window, "fired": []}

    # 1. Unresolved errors
    try:
        row = await db.execute(
            text(
                "SELECT COUNT(*) FROM error_logs "
                "WHERE resolved = FALSE "
                "  AND created_at >= NOW() - make_interval(mins => :w)"
            ),
            {"w": window},
        )
        unresolved = int(row.scalar() or 0)
    except Exception as exc:
        logger.debug("threshold_unresolved_query_failed: err=%s", exc)
        unresolved = 0
    summary["unresolved_errors"] = unresolved
    if unresolved >= settings.alert_unresolved_errors_threshold > 0:
        result = await _alerts.emit(
            kind="oncall.errors_unresolved",
            severity="warning",
            message=(
                f"{unresolved} unresolved error_logs row(s) in last {window}m "
                f"(threshold {settings.alert_unresolved_errors_threshold})"
            ),
            payload={"unresolved_count": unresolved, "window_minutes": window},
            dedup_key=f"oncall.errors_unresolved:{window}",
            db=db,
        )
        summary["fired"].append(("oncall.errors_unresolved", result))

    # 2. LLM rollup — cost + p95 latency
    try:
        rollup = await observability_rollups.llm_rollup(
            window_minutes=window, db=db,
        )
    except Exception as exc:
        logger.debug("threshold_llm_rollup_failed: err=%s", exc)
        rollup = {"total_cost_usd": 0.0, "by_model": []}

    cost_usd = float(rollup.get("total_cost_usd") or 0.0)
    summary["total_cost_usd"] = cost_usd
    if cost_usd > settings.alert_cost_window_usd_threshold > 0:
        result = await _alerts.emit(
            kind="cost.window_exceeded",
            severity="warning",
            message=(
                f"LLM cost ${cost_usd:.2f} in last {window}m exceeded "
                f"threshold ${settings.alert_cost_window_usd_threshold:.2f}"
            ),
            payload={
                "total_cost_usd": cost_usd, "window_minutes": window,
                "by_model_top": rollup.get("by_model", [])[:5],
            },
            dedup_key=f"cost.window_exceeded:{window}",
            db=db,
        )
        summary["fired"].append(("cost.window_exceeded", result))

    # 3. p95 latency — emit one alert per (provider, model) breach so
    #    operators can pinpoint which model is slow without parsing
    #    payload arrays. dedup_key carries the (provider, model) tuple
    #    so a sustained breach doesn't spam.
    p95_breaches = []
    threshold_ms = settings.alert_p95_latency_ms_threshold
    if threshold_ms > 0:
        for entry in rollup.get("by_model", []):
            p95 = int(entry.get("latency_ms_p95") or 0)
            if p95 > threshold_ms:
                provider = entry.get("provider") or "unknown"
                model = entry.get("model") or "unknown"
                p95_breaches.append({"provider": provider, "model": model, "p95_ms": p95})
                result = await _alerts.emit(
                    kind="latency.p95_exceeded",
                    severity="warning",
                    message=(
                        f"{provider}/{model} p95 latency {p95}ms exceeded "
                        f"threshold {threshold_ms}ms in last {window}m"
                    ),
                    payload={
                        "provider": provider, "model": model,
                        "p95_ms": p95, "threshold_ms": threshold_ms,
                        "window_minutes": window,
                    },
                    dedup_key=f"latency.p95_exceeded:{provider}:{model}:{window}",
                    db=db,
                )
                summary["fired"].append((f"latency.p95_exceeded:{provider}:{model}", result))
    summary["p95_breaches"] = p95_breaches
    return summary


async def tick() -> None:
    """One scheduler tick: refresh gauges + evaluate thresholds. Wrapped
    in a single short-lived async session so the periodic job is
    self-contained.

    Skips silently when ``alert_eval_enabled`` is False so operators can
    register a no-op tick (and revert to pull-only X.20) without removing
    the scheduler entry.
    """
    if not settings.alert_eval_enabled:
        return
    try:
        async with async_session() as db:
            await refresh_gauges(db)
            summary = await evaluate_thresholds(db)
            if summary.get("fired"):
                logger.info(
                    'event="threshold_eval_fired" count=%d window_m=%d',
                    len(summary["fired"]), summary["window_minutes"],
                )
            else:
                logger.debug(
                    'event="threshold_eval_clean" window_m=%d', summary["window_minutes"],
                )
    except Exception as exc:
        # Don't let scheduler tick errors crash the scheduler thread.
        logger.error('event="threshold_tick_failed" err=%s', exc)
