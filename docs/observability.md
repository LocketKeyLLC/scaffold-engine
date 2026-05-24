# Observability

The orchestrator exposes a Prometheus `/metrics` endpoint (no auth) for
scrape-based monitoring. This document covers what's emitted, how to
scrape it, and a starter alert-rule set. All metric names are prefixed
`scaffold_` for grouping in dashboards.

> **Status of this doc.** Authoritative for the metric inventory as of
> §17.193 (2026-05-20). New metrics added after that date should be
> added here in the same commit (see `app/observability/metrics.py` for
> the source of truth).

## Enabling

`/metrics` is gated on `settings.metrics_enabled` (`.env` key
`METRICS_ENABLED`), default **on**. When disabled the path returns
404 rather than an empty body so a misconfigured scraper fails loudly.

The endpoint is intentionally **unauthenticated** so Prometheus doesn't
need to carry an API key — the orchestrator binds to loopback by
default, and the bridge network restricts external reach. If you expose
the orchestrator publicly, gate `/metrics` at the reverse-proxy layer.

## Counters

| Metric | Labels | Source | Use |
|---|---|---|---|
| `scaffold_llm_calls_total` | `provider`, `model`, `success` | `cost_tracking.record_llm_call` | LLM call volume + success rate per (provider, model) |
| `scaffold_http_requests_total` | `method`, `path_template`, `status` | `PerformanceMiddleware` | HTTP RED metrics (rate / errors / duration) |
| `scaffold_alerts_emitted_total` | `kind`, `severity` | `observability.alerts.emit` | Per-kind alert fire rate; pair with `_suppressed_total` to compute dedup hit rate |
| `scaffold_alerts_suppressed_total` | `kind` | `observability.alerts.emit` | Alerts swallowed by cooldown — high values mean a runaway condition is being throttled |
| `scaffold_calibration_runs_total` | `status` (`ok`/`failed`/`watchdog_no_fire`) | `scripts/quarterly_calibration_pr.sh` + `calibration_watchdog` | Quarterly calibration cron health |

## Histograms

| Metric | Labels | Buckets (seconds) |
|---|---|---|
| `scaffold_llm_latency_seconds` | `provider`, `model` | `0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600, 1800` |
| `scaffold_http_request_duration_seconds` | `method`, `path_template` | `0.005, 0.025, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60` |

Histograms emit `_bucket`, `_count`, and `_sum` series; use
`histogram_quantile` in PromQL for percentiles.

## Gauges

| Metric | Labels | Refresher | Notes |
|---|---|---|---|
| `scaffold_jobs_by_status` | `status` | `thresholds.refresh_gauges` (every alert-eval tick) | Cleared each tick so empty statuses don't stay stuck at the last value |
| `scaffold_research_sessions_running` | — | `thresholds.refresh_gauges` | Count of `research_sessions` rows still in `running` state |
| `scaffold_unresolved_errors_window` | — | `thresholds.refresh_gauges` | `error_logs WHERE resolved=FALSE AND created_at >= NOW() - eval_window` |
| `scaffold_executor_concurrency_inflight` | — | live (read on scrape) | DAG nodes currently holding a slot in the global semaphore |
| `scaffold_executor_concurrency_cap` | — | set on every scrape | Configured `execution_global_concurrency` |
| `scaffold_calibration_last_success_timestamp` | — | calibration CLI | Unix epoch seconds; `0` if the cron has never succeeded on this host |
| `scaffold_calibration_last_failure_timestamp` | — | calibration CLI | Unix epoch seconds; `0` if the cron has never failed |

## Sample Prometheus scrape config

```yaml
scrape_configs:
  - job_name: scaffold-engine
    metrics_path: /metrics
    scrape_interval: 30s
    scrape_timeout: 10s
    static_configs:
      - targets: ['scaffold-orchestrator:8000']
        # Or 'host.docker.internal:8000' from a sibling container,
        # or '127.0.0.1:8000' if running Prometheus on the host.
    relabel_configs:
      # Strip the docker-compose service-name port suffix from the
      # default `instance` label for cleaner dashboards.
      - source_labels: [__address__]
        regex: '([^:]+):.+'
        target_label: instance
```

Recommended scrape interval is **30s**. The histograms have minute-scale
buckets so anything faster is wasted detail; anything slower loses
resolution on the 1-minute alert rules below.

## Starter alert rule pack

These five rules cover the highest-signal failure modes. Copy into
`/etc/prometheus/rules.d/scaffold.yml` and reload.

```yaml
groups:
  - name: scaffold-engine
    interval: 30s
    rules:

      # 1. HTTP error rate — 5xx fraction over the last 5 min > 5%.
      #    The orchestrator's own structured errors are 4xx so a 5xx
      #    spike is almost always an unhandled exception or upstream-down.
      - alert: ScaffoldHttpServerErrorRateHigh
        expr: |
          sum(rate(scaffold_http_requests_total{status=~"5.."}[5m]))
          /
          sum(rate(scaffold_http_requests_total[5m])) > 0.05
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Scaffold 5xx rate above 5% over 5 min"
          description: "Check /health, recent error_logs rows, and docker logs scaffold-orchestrator."

      # 2. LLM call failure rate — same shape, different counter.
      - alert: ScaffoldLlmCallFailureRateHigh
        expr: |
          sum(rate(scaffold_llm_calls_total{success="false"}[10m]))
          /
          sum(rate(scaffold_llm_calls_total[10m])) > 0.10
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "Scaffold LLM call failure rate above 10% over 10 min"
          description: "Likely Ollama unreachable or a model overloaded. Check /health."

      # 3. Per-model p95 LLM latency over budget. The internal threshold
      #    alert (cost.window_exceeded / latency.p95_exceeded) already
      #    covers this from the orchestrator side; this rule is the
      #    Prometheus-side fallback for operators not running the
      #    alert-DB consumer.
      - alert: ScaffoldLlmP95LatencyHigh
        expr: |
          histogram_quantile(
            0.95,
            sum(rate(scaffold_llm_latency_seconds_bucket[5m]))
              by (le, provider, model)
          ) > 120
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "{{ $labels.provider }}/{{ $labels.model }} p95 latency > 120s"

      # 4. Calibration cron didn't fire OR didn't succeed in 100 days
      #    (cron runs quarterly = 90 days; 100 gives a 10-day grace).
      - alert: ScaffoldCalibrationStale
        expr: |
          (time() - scaffold_calibration_last_success_timestamp) > (100 * 86400)
          unless scaffold_calibration_last_success_timestamp == 0
        for: 1h
        labels:
          severity: warning
        annotations:
          summary: "Scaffold quarterly calibration cron has not succeeded in 100+ days"

      # 5. Executor concurrency permanently capped. Sustained at-cap
      #    inflight + queueing means /execute/all requests are stacking
      #    up — either a load problem or a stuck node holding the slot.
      - alert: ScaffoldExecutorPermanentlySaturated
        expr: |
          (scaffold_executor_concurrency_inflight
            / scaffold_executor_concurrency_cap) >= 1.0
        for: 30m
        labels:
          severity: warning
        annotations:
          summary: "Scaffold executor at 100% capacity for 30+ minutes"
          description: "Check `POST /jobs/cleanup` for orphans and `/health` for stuck node_orphan rows."

      # 6. §17.257 — Reranker p95 latency exceeds the operator-
      #    tolerance budget. Threshold 60s catches the unusually-slow
      #    case without false-alarming operators who deliberately
      #    raised RERANK_DOC_TRUNCATE (the §17.238 matrix's `trunc=2000`
      #    cell legitimately runs at ~52s/call). `by (max_candidates,
      #    doc_truncate, le)` keeps the rule firing per-knob-config so
      #    an operator who passes `{"max_candidates":20,"doc_truncate":2000}`
      #    via the §17.234/§17.252 per-request override sees the alert
      #    tagged with that specific config, not a fleet-wide average.
      - alert: ScaffoldRerankerP95LatencyHigh
        expr: |
          histogram_quantile(
            0.95,
            sum(rate(scaffold_reranker_latency_seconds_bucket[10m]))
              by (le, max_candidates, doc_truncate)
          ) > 60
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "Reranker p95 > 60s at (max_candidates={{ $labels.max_candidates }}, doc_truncate={{ $labels.doc_truncate }})"
          description: "Check `reranker_decision` log lines (§17.254) for the same (max_candidates, doc_truncate) tuple to confirm the load. If the config is intentional (deep-context research), raise this rule's threshold. Otherwise inspect `/health` for orchestrator memory pressure or container restarts (§17.232)."
```

## Cross-references

- `app/observability/metrics.py` — emitter definitions (source of truth).
- `app/observability/thresholds.py` — internal threshold alert evaluator.
  Fires `cost.window_exceeded` / `latency.p95_exceeded` /
  `oncall.errors_unresolved` to the `system_alerts` table; the
  Prometheus alert rules above are a complementary external pathway.
  The §17.257 reranker p95 alert is **Prometheus-only** — there's no
  internal-check equivalent yet because reranker calls aren't logged
  to a DB rollup the way LLM calls are (`observability_rollups.by_model`).
  See §17.258 candidate A in the OVERVIEW for the DB-backed mirror.
- `app/observability/alerts.py` — alert emission + dedup. Read by the
  `/observability/alerts` endpoint and the `scaffold alerts` CLI.
- `OVERVIEW.md` §17.132 (embedding cache pressure), §17.135 (embedder
  drift detection), §17.140-§17.142 (sim-sidecar audit rows),
  §17.256 (reranker latency histogram), §17.257 (this entry) for the
  history of how each metric came to be.
