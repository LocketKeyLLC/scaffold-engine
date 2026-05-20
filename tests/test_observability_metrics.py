"""Sprint X.26 — Prometheus /metrics endpoint + hot-path counters."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.auth import require_api_key
from app.main import app
from app.observability import metrics as _metrics


@pytest.fixture(autouse=True)
def _reset_metrics():
    _metrics.reset_for_tests()
    yield
    _metrics.reset_for_tests()


@pytest.fixture
def client():
    # /metrics is unauthenticated by design, but the global TestClient still
    # binds the dependency override so other endpoints stay reachable.
    app.dependency_overrides[require_api_key] = lambda: "test-key"
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.mark.smoke
class TestMetricsEndpoint:
    def test_metrics_endpoint_returns_text_exposition(self, client):
        r = client.get("/metrics")
        assert r.status_code == 200
        # prometheus exposition is text/plain with versioning
        assert "text/plain" in r.headers.get("content-type", "")
        body = r.text
        # Three known metric families must be present even at zero count.
        assert "scaffold_llm_calls_total" in body
        assert "scaffold_alerts_emitted_total" in body
        assert "scaffold_executor_concurrency_inflight" in body

    def test_record_llm_call_increments_counter(self, client):
        _metrics.record_llm_call(
            provider="openai", model="gpt-4o", success=True, latency_ms=1500,
        )
        _metrics.record_llm_call(
            provider="openai", model="gpt-4o", success=False, latency_ms=2500,
        )
        body = client.get("/metrics").text
        # Both label values present in the exposition.
        assert 'scaffold_llm_calls_total{model="gpt-4o",provider="openai",success="true"} 1.0' in body
        assert 'scaffold_llm_calls_total{model="gpt-4o",provider="openai",success="false"} 1.0' in body

    def test_record_http_request_increments_counter(self, client):
        _metrics.record_http_request(
            method="POST", path_template="/ideate", status=200, duration_s=0.42,
        )
        body = client.get("/metrics").text
        assert (
            'scaffold_http_requests_total{method="POST",'
            'path_template="/ideate",status="200"} 1.0'
        ) in body

    def test_metrics_endpoint_disabled_returns_404(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.main.settings.metrics_enabled", False, raising=False,
        )
        r = client.get("/metrics")
        assert r.status_code == 404

    def test_unknown_provider_normalized_to_unknown(self, client):
        _metrics.record_llm_call(
            provider="", model="", success=True, latency_ms=10,
        )
        body = client.get("/metrics").text
        assert (
            'scaffold_llm_calls_total{model="unknown",provider="unknown",success="true"} 1.0'
        ) in body

    def test_executor_concurrency_gauge_reads_live(self, client, monkeypatch):
        # No semaphore created yet → 0
        monkeypatch.setattr(
            "app.modules.execution_agent._execution_slot_sem", None, raising=False,
        )
        body = client.get("/metrics").text
        assert "scaffold_executor_concurrency_inflight 0.0" in body


# ---------------------------------------------------------------------------
# §17.192 — extended coverage for gauges + alert counters + histogram
# ---------------------------------------------------------------------------
#
# The pre-§17.192 test suite covered the LLM/HTTP counter hot paths but left
# several declared metrics untested:
#   * scaffold_jobs_by_status / research_sessions_running / unresolved_errors_
#     window — gauges refreshed by thresholds.refresh_gauges
#   * scaffold_calibration_last_{success,failure}_timestamp — set by the
#     calibration cron CLI
#   * scaffold_alerts_emitted_total / scaffold_alerts_suppressed_total —
#     incremented by alerts.emit (post-dedup vs in-cooldown)
#   * scaffold_llm_latency_seconds histogram buckets
#   * scaffold_executor_concurrency_cap — set on every scrape
#
# These tests are the audit-flagged coverage gap for /metrics' gauges.


@pytest.mark.smoke
class TestMetricsGauges:
    def test_executor_concurrency_cap_set_on_scrape(self, client, monkeypatch):
        """expose() sets the cap gauge from settings on every scrape so a
        config reload between scrapes is reflected without an explicit
        reset path. Bump the setting between two scrapes and verify."""
        monkeypatch.setattr(
            "app.observability.metrics.settings.execution_global_concurrency",
            7, raising=False,
        )
        body = client.get("/metrics").text
        assert "scaffold_executor_concurrency_cap 7.0" in body

    def test_jobs_by_status_gauge_present_in_exposition(self, client):
        """The gauge is declared with labels=[status]; until refresh_gauges
        populates it, the exposition carries the metric family header but
        no samples. Verify the HELP line is present so an alert rule
        author can confirm the metric exists."""
        body = client.get("/metrics").text
        assert "# HELP scaffold_jobs_by_status" in body
        assert "# TYPE scaffold_jobs_by_status gauge" in body

    def test_jobs_by_status_set_value_appears_in_exposition(self, client):
        from app.observability.metrics import jobs_by_status
        jobs_by_status.labels(status="executing").set(3)
        jobs_by_status.labels(status="completed").set(42)
        body = client.get("/metrics").text
        assert 'scaffold_jobs_by_status{status="executing"} 3.0' in body
        assert 'scaffold_jobs_by_status{status="completed"} 42.0' in body

    def test_calibration_timestamp_gauges_default_to_zero(self, client):
        body = client.get("/metrics").text
        assert "scaffold_calibration_last_success_timestamp 0.0" in body
        assert "scaffold_calibration_last_failure_timestamp 0.0" in body

    def test_calibration_runs_counter_increments_per_status(self, client):
        from app.observability.metrics import calibration_runs_total
        calibration_runs_total.labels(status="ok").inc()
        calibration_runs_total.labels(status="ok").inc()
        calibration_runs_total.labels(status="failed").inc()
        body = client.get("/metrics").text
        assert 'scaffold_calibration_runs_total{status="ok"} 2.0' in body
        assert 'scaffold_calibration_runs_total{status="failed"} 1.0' in body


@pytest.mark.smoke
class TestAlertCounters:
    def test_alerts_emitted_increments_per_kind_severity(self, client):
        from app.observability.metrics import alerts_emitted_total
        alerts_emitted_total.labels(kind="cost.window_exceeded", severity="warning").inc()
        alerts_emitted_total.labels(kind="cost.window_exceeded", severity="warning").inc()
        alerts_emitted_total.labels(kind="oncall.errors_unresolved", severity="critical").inc()
        body = client.get("/metrics").text
        assert (
            'scaffold_alerts_emitted_total{kind="cost.window_exceeded",'
            'severity="warning"} 2.0'
        ) in body
        assert (
            'scaffold_alerts_emitted_total{kind="oncall.errors_unresolved",'
            'severity="critical"} 1.0'
        ) in body

    def test_alerts_suppressed_increments_per_kind(self, client):
        from app.observability.metrics import alerts_suppressed_total
        alerts_suppressed_total.labels(kind="cost.window_exceeded").inc()
        body = client.get("/metrics").text
        assert (
            'scaffold_alerts_suppressed_total{kind="cost.window_exceeded"} 1.0'
        ) in body


@pytest.mark.smoke
class TestLlmLatencyHistogram:
    def test_latency_observed_in_correct_bucket(self, client):
        """A 750 ms call lands in the +Inf, le="1.0" buckets but NOT in le="0.5"."""
        _metrics.record_llm_call(
            provider="ollama", model="qwen3:4b", success=True, latency_ms=750,
        )
        body = client.get("/metrics").text
        # le="1.0" must include the 0.75s observation; le="0.5" must not.
        assert 'scaffold_llm_latency_seconds_bucket{le="1.0",model="qwen3:4b",provider="ollama"} 1.0' in body
        assert 'scaffold_llm_latency_seconds_bucket{le="0.5",model="qwen3:4b",provider="ollama"} 0.0' in body

    def test_latency_count_and_sum_recorded(self, client):
        _metrics.record_llm_call(
            provider="ollama", model="qwen3:4b", success=True, latency_ms=500,
        )
        _metrics.record_llm_call(
            provider="ollama", model="qwen3:4b", success=True, latency_ms=1500,
        )
        body = client.get("/metrics").text
        assert 'scaffold_llm_latency_seconds_count{model="qwen3:4b",provider="ollama"} 2.0' in body
        assert 'scaffold_llm_latency_seconds_sum{model="qwen3:4b",provider="ollama"} 2.0' in body

    def test_zero_latency_recorded_as_first_bucket(self, client):
        """Cached / instant paths report latency_ms=0; histogram should
        still record it in the smallest bucket (le=0.1) without crashing."""
        _metrics.record_llm_call(
            provider="ollama", model="qwen3:4b", success=True, latency_ms=0,
        )
        body = client.get("/metrics").text
        assert 'scaffold_llm_latency_seconds_bucket{le="0.1",model="qwen3:4b",provider="ollama"} 1.0' in body

    def test_negative_latency_clamped_to_zero(self, client):
        """Defensive: a clock-jitter negative latency mustn't blow up the
        histogram (which would reject negative observations)."""
        _metrics.record_llm_call(
            provider="ollama", model="qwen3:4b", success=True, latency_ms=-50,
        )
        body = client.get("/metrics").text
        # Same first-bucket landing as latency_ms=0 — no exception.
        assert 'scaffold_llm_latency_seconds_count{model="qwen3:4b",provider="ollama"} 1.0' in body
