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
