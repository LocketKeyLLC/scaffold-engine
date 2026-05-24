"""Sprint X.26 — Prometheus metrics surface.

Exposes a process-local `CollectorRegistry` and the metric instances that
hot paths increment. `expose()` returns a Starlette `Response` carrying
the standard exposition format; mounted at `settings.metrics_path` from
`app/main.py`.

Hook points (the only places that touch this module):

  * `app/utils/cost_tracking.record_llm_call` — LLM call counter + latency
  * `app/middleware/performance.PerformanceMiddleware` — HTTP histogram
  * `app/observability/alerts.emit` — alert counter
  * `app/observability/thresholds.refresh_gauges` — system snapshot gauges
    (jobs/sessions/concurrency) refreshed each eval tick

The executor concurrency gauge uses a callback so its value is read live
at scrape time rather than mirrored on every acquire/release.
"""
from __future__ import annotations

import logging
from typing import Optional

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.responses import Response

from app.config import settings

logger = logging.getLogger("scaffold.metrics")

# Dedicated registry — keeps test isolation simple (we never collide with
# the prometheus_client default REGISTRY) and lets us hand a clean process
# view to scrapers without the SDK's process-collector noise.
registry = CollectorRegistry()


# ── Counters ─────────────────────────────────────────────────────────

llm_calls_total = Counter(
    "scaffold_llm_calls_total",
    "Total LLM calls recorded by cost_tracking.record_llm_call.",
    ["provider", "model", "success"],
    registry=registry,
)

llm_latency_seconds = Histogram(
    "scaffold_llm_latency_seconds",
    "LLM call wall-clock latency (seconds), labeled by (provider, model).",
    ["provider", "model"],
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600, 1800),
    registry=registry,
)

http_requests_total = Counter(
    "scaffold_http_requests_total",
    "Total HTTP requests handled by the orchestrator.",
    ["method", "path_template", "status"],
    registry=registry,
)

http_request_duration_seconds = Histogram(
    "scaffold_http_request_duration_seconds",
    "HTTP request wall-clock duration (seconds).",
    ["method", "path_template"],
    buckets=(0.005, 0.025, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
    registry=registry,
)

# §17.256 — reranker latency keyed on the EFFECTIVE knobs used per call.
# Buckets calibrated against this hardware (T480, 4-core CPU): the
# §17.238 Pareto matrix observed wall times from ~5 s (max=5, trunc=250)
# to ~234 s (max=20, trunc=2000). Buckets cover that whole range plus
# headroom for outliers and skip-rerank zero-latency observations.
reranker_latency_seconds = Histogram(
    "scaffold_reranker_latency_seconds",
    "Reranker wall-clock latency (seconds), labeled by effective knob values.",
    ["max_candidates", "doc_truncate"],
    buckets=(0.5, 1, 2.5, 5, 10, 20, 30, 60, 120, 300),
    registry=registry,
)

alerts_emitted_total = Counter(
    "scaffold_alerts_emitted_total",
    "Alerts emitted by app.observability.alerts.emit (post-dedup).",
    ["kind", "severity"],
    registry=registry,
)

alerts_suppressed_total = Counter(
    "scaffold_alerts_suppressed_total",
    "Alerts suppressed by the dedup/cooldown gate.",
    ["kind"],
    registry=registry,
)

calibration_runs_total = Counter(
    "scaffold_calibration_runs_total",
    "Quarterly calibration cron runs by status.",
    ["status"],  # ok | failed | watchdog_no_fire
    registry=registry,
)


# ── Gauges (refreshed on eval tick by thresholds.refresh_gauges) ─────

jobs_by_status = Gauge(
    "scaffold_jobs_by_status",
    "Job count by current status.",
    ["status"],
    registry=registry,
)

research_sessions_running = Gauge(
    "scaffold_research_sessions_running",
    "research_sessions rows currently in 'running' state.",
    registry=registry,
)

unresolved_errors_window = Gauge(
    "scaffold_unresolved_errors_window",
    "Unresolved error_logs rows in the configured eval window.",
    registry=registry,
)

calibration_last_success_timestamp = Gauge(
    "scaffold_calibration_last_success_timestamp",
    "Unix epoch seconds of last successful calibration cron run; 0 if never.",
    registry=registry,
)

calibration_last_failure_timestamp = Gauge(
    "scaffold_calibration_last_failure_timestamp",
    "Unix epoch seconds of last failed calibration cron run; 0 if never.",
    registry=registry,
)


# Live executor concurrency — read from the lazily-initialized semaphore
# at scrape time. Touching the private `_value` is acceptable here:
# telemetry is read-only, and asyncio.Semaphore exposes no public
# inflight count.
def _executor_inflight() -> float:
    try:
        from app.modules.execution_agent import _execution_slot_sem
        if _execution_slot_sem is None:
            return 0.0
        cap = settings.execution_global_concurrency
        return float(max(0, cap - _execution_slot_sem._value))
    except Exception:
        return 0.0


executor_concurrency_inflight = Gauge(
    "scaffold_executor_concurrency_inflight",
    "execute_all_nodes runs currently holding a global concurrency slot.",
    registry=registry,
)
executor_concurrency_inflight.set_function(_executor_inflight)


executor_concurrency_cap = Gauge(
    "scaffold_executor_concurrency_cap",
    "Configured value of execution_global_concurrency.",
    registry=registry,
)


# ── Hooks ────────────────────────────────────────────────────────────

def record_llm_call(*, provider: str, model: str, success: bool, latency_ms: int) -> None:
    """Hot-path hook from cost_tracking.record_llm_call.

    Cardinality protection: provider/model are bounded by the
    ``model_costs`` seed (~dozens); free-form values would explode the
    label space, so callers normalize empty values to 'unknown' upstream.
    """
    try:
        llm_calls_total.labels(
            provider=provider or "unknown",
            model=model or "unknown",
            success="true" if success else "false",
        ).inc()
        # latency_ms can be 0 for cached or otherwise instant paths; the
        # histogram still records the bucket.
        llm_latency_seconds.labels(
            provider=provider or "unknown",
            model=model or "unknown",
        ).observe(max(0, int(latency_ms)) / 1000.0)
    except Exception:
        # Metrics must never break the LLM call path.
        logger.debug("record_llm_call_metrics_failed", exc_info=True)


def record_reranker_call(*, max_candidates: int, doc_truncate: int, latency_ms: float) -> None:
    """§17.256 — hot-path hook from rag_pipeline._rerank.

    Emits ``scaffold_reranker_latency_seconds`` keyed on the EFFECTIVE
    reranker knobs (§17.234 / §17.252) so operators can build dashboards
    that compare latency across knob configurations:

        scaffold_reranker_latency_seconds{max_candidates="5",doc_truncate="250"}

    Cardinality protection: max_candidates and doc_truncate are bounded
    by the Pydantic validators in ``RagInput`` (1-512 × 100-20000) and
    by operator-side `.env` choice. In practice operators use a small
    set of values (~5-10 each), so cardinality stays in the dozens of
    series. If a future operator workflow widens that, switch to a
    bucketed label (e.g. "low"/"med"/"high") — see §17.256's inline
    comment for the trade-off.

    Mirrors ``record_llm_call`` / ``record_http_request`` shape: metric
    write must never raise; the caller's hot path is more important
    than the dashboard.
    """
    try:
        reranker_latency_seconds.labels(
            max_candidates=str(int(max_candidates)),
            doc_truncate=str(int(doc_truncate)),
        ).observe(max(0.0, float(latency_ms)) / 1000.0)
    except Exception:
        logger.debug("record_reranker_call_metrics_failed", exc_info=True)


def record_http_request(*, method: str, path_template: str, status: int, duration_s: float) -> None:
    """Hot-path hook from PerformanceMiddleware. ``path_template`` should
    be the route template (e.g. ``/jobs/{job_id}/costs``) when available
    so per-id paths don't blow up label cardinality.
    """
    try:
        http_requests_total.labels(
            method=method, path_template=path_template, status=str(status),
        ).inc()
        http_request_duration_seconds.labels(
            method=method, path_template=path_template,
        ).observe(max(0.0, float(duration_s)))
    except Exception:
        logger.debug("record_http_request_metrics_failed", exc_info=True)


# ── Endpoint handler ─────────────────────────────────────────────────

async def expose(_request) -> Response:
    """Starlette handler — returns the current registry snapshot.

    Set the cap gauge here so a config reload between scrapes is reflected
    without an explicit reset path; the cap is cheap to read and never
    stale.
    """
    executor_concurrency_cap.set(float(settings.execution_global_concurrency))
    payload = generate_latest(registry)
    return Response(content=payload, media_type=CONTENT_TYPE_LATEST)


def reset_for_tests() -> None:
    """Test-only — clear all metric values. The registry itself is kept so
    test imports of metric instances remain valid."""
    for collector in list(registry._collector_to_names.keys()):
        try:
            collector._metrics.clear()  # multi-label collectors
        except Exception:
            pass
        try:
            collector._value.set(0)  # single-value gauges
        except Exception:
            pass
