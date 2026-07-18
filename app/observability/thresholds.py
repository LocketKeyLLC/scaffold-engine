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


# ── Embedding-cache pressure: prior-tick snapshot ───────────────────
#
# §17.132 — the embedding cache's hit / miss / eviction counters are
# monotonic over the process lifetime, so a single read tells us
# nothing about *recent* pressure. This dict holds the previous tick's
# values; each tick subtracts to get the interval rate. Reset on
# orchestrator restart (the cache itself resets too, so the dance is
# consistent). The first tick after restart writes the baseline + emits
# nothing.
_prev_embedding_snapshot: dict[str, int] = {}


def _reset_embedding_snapshot() -> None:
    """Test seam: clear the baseline so the next tick re-establishes it."""
    _prev_embedding_snapshot.clear()


async def refresh_gauges(db) -> int | None:
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

    # §17.611 (audit #23) — return this count so tick() can hand it to
    # evaluate_thresholds instead of re-running the byte-identical query.
    unresolved_count: int | None = None
    try:
        row = await db.execute(
            text(
                "SELECT COUNT(*) FROM error_logs "
                "WHERE resolved = FALSE "
                "  AND created_at >= NOW() - make_interval(mins => :w)"
            ),
            {"w": settings.alert_eval_window_minutes},
        )
        unresolved_count = int(row.scalar() or 0)
        _metrics.unresolved_errors_window.set(unresolved_count)
    except Exception as exc:
        logger.debug("refresh_unresolved_errors_failed: err=%s", exc)
    return unresolved_count


async def evaluate_thresholds(db, *, unresolved_count: int | None = None) -> dict[str, Any]:
    """Evaluate thresholds, fire alerts where breached, return a summary
    dict for tests + structured logging. Never raises.

    §17.611 (audit #23) — ``unresolved_count`` lets ``tick()`` pass the value
    already computed by ``refresh_gauges`` so the identical count query isn't
    run twice per tick. When None (standalone call), it is queried here.
    """
    window = settings.alert_eval_window_minutes
    summary: dict[str, Any] = {"window_minutes": window, "fired": []}

    # 1. Unresolved errors
    if unresolved_count is not None:
        unresolved = unresolved_count
    else:
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

    # 4. Embedding-cache pressure. Fires only when BOTH:
    #    (a) interval evictions ≥ alert_embedding_evictions_threshold
    #    (b) interval hit-rate < alert_embedding_hit_rate_floor
    # so cold-start (low hit rate, zero evictions) and steady-state
    # (high hit rate, occasional evictions) both stay silent.
    cache_summary = await _check_embedding_cache_pressure(
        db=db, summary=summary, window_min=window,
    )
    if cache_summary is not None:
        summary["embedding_cache"] = cache_summary

    return summary


async def _check_embedding_cache_pressure(
    *, db, summary: dict[str, Any], window_min: int,
) -> dict[str, Any] | None:
    """Compute interval cache deltas + maybe emit the pressure alert.

    Returns a small audit dict that the caller folds into the tick
    summary, or None when the check is disabled by configuration. Never
    raises — cache stats are best-effort and must never break the tick.
    """
    evict_threshold = settings.alert_embedding_evictions_threshold
    hit_floor = settings.alert_embedding_hit_rate_floor
    if evict_threshold <= 0:
        # Operator disabled the alert. Don't even compute deltas — also
        # do NOT update the snapshot so re-enabling later doesn't carry
        # a stale baseline forward.
        return {"disabled": True}

    try:
        from app.utils.embedding_cache import get_cache
        stats = get_cache().stats
    except Exception as exc:
        logger.debug("threshold_embedding_stats_failed: err=%s", exc)
        return None

    cur = {
        "hits": int(stats.get("hits") or 0),
        "misses": int(stats.get("misses") or 0),
        "evictions": int(stats.get("evictions") or 0),
    }

    if not _prev_embedding_snapshot:
        # First tick of this process. Establish baseline + skip emit.
        _prev_embedding_snapshot.update(cur)
        return {"baseline_established": True, **cur}

    d_hits = max(0, cur["hits"] - _prev_embedding_snapshot.get("hits", 0))
    d_misses = max(0, cur["misses"] - _prev_embedding_snapshot.get("misses", 0))
    d_evictions = max(0, cur["evictions"] - _prev_embedding_snapshot.get("evictions", 0))
    d_total = d_hits + d_misses
    interval_hit_rate = (d_hits / d_total) if d_total > 0 else None

    audit = {
        "delta_hits": d_hits,
        "delta_misses": d_misses,
        "delta_evictions": d_evictions,
        "interval_hit_rate": (
            round(interval_hit_rate, 4) if interval_hit_rate is not None else None
        ),
        "memory_size": int(stats.get("memory_size") or 0),
        "fired": False,
    }

    pressure = (
        d_evictions >= evict_threshold
        and interval_hit_rate is not None
        and interval_hit_rate < hit_floor
    )
    if pressure:
        result = await _alerts.emit(
            kind="cache.embedding_pressure",
            severity="warning",
            message=(
                f"embedding cache: {d_evictions} evictions / interval, "
                f"hit_rate={interval_hit_rate:.2%} below floor "
                f"{hit_floor:.0%} — consider raising "
                f"embedding_cache_memory_size (currently "
                f"{settings.embedding_cache_memory_size})"
            ),
            payload={
                "delta_hits": d_hits, "delta_misses": d_misses,
                "delta_evictions": d_evictions,
                "interval_hit_rate": round(interval_hit_rate, 4),
                "evictions_threshold": evict_threshold,
                "hit_rate_floor": hit_floor,
                "memory_size_current": int(stats.get("memory_size") or 0),
                "memory_size_setting": settings.embedding_cache_memory_size,
                "window_minutes": window_min,
            },
            dedup_key="cache.embedding_pressure",
            db=db,
        )
        audit["fired"] = True
        summary["fired"].append(("cache.embedding_pressure", result))

    # Always update snapshot after eval so the next tick has the latest
    # baseline regardless of whether the alert fired.
    _prev_embedding_snapshot.update(cur)
    return audit


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
            unresolved = await refresh_gauges(db)
            summary = await evaluate_thresholds(db, unresolved_count=unresolved)
            if summary.get("fired"):
                logger.info(
                    'event="threshold_eval_fired" count=%d window_m=%d',
                    len(summary["fired"]), summary["window_minutes"],
                )
            else:
                # INFO (not DEBUG) so the operator has a positive heartbeat
                # at the configured log_level. The APScheduler executor log
                # already proves dispatch happened; this proves the body ran
                # to completion. unresolved/cost surfaced so a clean tick
                # still answers "what did it see?" at a glance.
                logger.info(
                    'event="threshold_eval_clean" window_m=%d '
                    'unresolved=%d cost_usd=%.4f p95_breaches=%d',
                    summary["window_minutes"],
                    int(summary.get("unresolved_errors") or 0),
                    float(summary.get("total_cost_usd") or 0.0),
                    len(summary.get("p95_breaches") or []),
                )
    except Exception as exc:
        # Don't let scheduler tick errors crash the scheduler thread.
        logger.error('event="threshold_tick_failed" err=%s', exc)
