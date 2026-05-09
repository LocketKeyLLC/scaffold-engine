"""Sprint X.20 — observability rollup helpers + endpoints.

Coverage:
  * llm_rollup: aggregates by (provider, model) with totals + percentiles;
    filter parameters thread through; fail-open on DB error.
  * recent_errors: filter by resolved + since_minutes; ordering;
    fail-open on DB error.
  * recent_jobs_costs: cost-DESC ordering; LEFT JOIN preserves jobs
    with zero LLM calls; fail-open on DB error.
  * Endpoints: GET /observability/{llm,errors,jobs} return 200 with the
    helper's payload shape; query params validate.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.auth import require_api_key
from app.main import app
from app.modules.observability_rollups import (
    llm_rollup, recent_errors, recent_jobs_costs,
)


def _mock_db_rows(rows: list[dict]):
    """Build an AsyncMock db whose execute() returns a result with
    .mappings().all() == rows."""
    result = MagicMock()
    mappings = MagicMock()
    mappings.all.return_value = rows
    result.mappings.return_value = mappings
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    return db


def _mock_db_raises(exc: Exception):
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=exc)
    return db


# ---------------------------------------------------------------------------
# llm_rollup
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestLlmRollup:
    async def test_aggregates_with_percentiles(self):
        db = _mock_db_rows([
            {
                "provider": "openai", "model": "gpt-4o",
                "calls": 10, "successes": 9, "failures": 1,
                "cost_usd": 0.50, "prompt_tokens": 5000, "completion_tokens": 2000,
                "latency_ms_sum": 25000,
                "latency_ms_p50": 2300, "latency_ms_p95": 4800, "latency_ms_p99": 5100,
            },
            {
                "provider": "ollama", "model": "qwen3:4b",
                "calls": 50, "successes": 50, "failures": 0,
                "cost_usd": 0.0, "prompt_tokens": 12000, "completion_tokens": 8000,
                "latency_ms_sum": 90000,
                "latency_ms_p50": 1700, "latency_ms_p95": 2200, "latency_ms_p99": 2400,
            },
        ])

        result = await llm_rollup(window_minutes=60, db=db)

        assert result["window_minutes"] == 60
        assert result["total_calls"] == 60
        assert result["total_cost_usd"] == pytest.approx(0.50)
        assert len(result["by_model"]) == 2
        assert result["by_model"][0]["provider"] == "openai"
        assert result["by_model"][0]["latency_ms_p95"] == 4800

    async def test_filter_params_thread_through_to_sql(self):
        """provider + model filters must reach the bound SQL params so the
        WHERE clause actually narrows. Catches accidental drops."""
        captured = {}

        async def _capture(stmt, params=None):
            captured.update(params or {})
            r = MagicMock()
            r.mappings.return_value = MagicMock()
            r.mappings.return_value.all.return_value = []
            return r

        db = AsyncMock()
        db.execute = _capture
        await llm_rollup(
            window_minutes=120, provider="openai", model="gpt-4o", db=db,
        )
        assert captured["window_minutes"] == 120
        assert captured["provider_filter"] == "openai"
        assert captured["model_filter"] == "gpt-4o"

    async def test_omitted_filters_pass_none_so_sql_disables_them(self):
        """The SQL is `(:provider_filter IS NULL OR provider = ...)` —
        passing None must disable the filter, not match the literal 'None'."""
        captured = {}

        async def _capture(stmt, params=None):
            captured.update(params or {})
            r = MagicMock()
            r.mappings.return_value = MagicMock()
            r.mappings.return_value.all.return_value = []
            return r

        db = AsyncMock()
        db.execute = _capture
        await llm_rollup(window_minutes=60, db=db)
        assert captured["provider_filter"] is None
        assert captured["model_filter"] is None

    async def test_db_error_fails_open(self):
        db = _mock_db_raises(RuntimeError("relation does not exist"))
        result = await llm_rollup(window_minutes=60, db=db)
        assert result == {
            "window_minutes": 60,
            "total_calls": 0,
            "total_cost_usd": 0.0,
            "by_model": [],
        }


# ---------------------------------------------------------------------------
# recent_errors
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestRecentErrors:
    async def test_returns_rows_with_iso_timestamps(self):
        eid = uuid4()
        jid = uuid4()
        nid = uuid4()
        now = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
        db = _mock_db_rows([{
            "id": eid, "job_id": jid, "node_id": nid,
            "error_type": "timeout", "error_message": "model took too long",
            "model_used": "qwen3:4b", "retry_count": 1,
            "recovery_action": "retry", "recovery_model": None,
            "resolved": False, "resolution": None,
            "created_at": now, "resolved_at": None,
        }])

        result = await recent_errors(resolved=False, limit=10, db=db)

        assert result["count"] == 1
        e = result["errors"][0]
        assert e["id"] == str(eid)
        assert e["job_id"] == str(jid)
        assert e["error_type"] == "timeout"
        assert e["resolved"] is False
        assert e["created_at"] == now.isoformat()
        assert e["resolved_at"] is None

    async def test_filter_params_thread_through(self):
        captured = {}

        async def _capture(stmt, params=None):
            captured.update(params or {})
            r = MagicMock()
            r.mappings.return_value = MagicMock()
            r.mappings.return_value.all.return_value = []
            return r

        db = AsyncMock()
        db.execute = _capture
        await recent_errors(resolved=False, since_minutes=120, limit=25, db=db)
        assert captured["resolved_filter"] is False
        assert captured["since_minutes"] == 120
        assert captured["limit"] == 25

    async def test_omitted_filters_pass_none(self):
        """resolved=None must pass through (not a boolean) so the SQL's
        IS NULL branch fires and the filter is effectively disabled."""
        captured = {}

        async def _capture(stmt, params=None):
            captured.update(params or {})
            r = MagicMock()
            r.mappings.return_value = MagicMock()
            r.mappings.return_value.all.return_value = []
            return r

        db = AsyncMock()
        db.execute = _capture
        await recent_errors(db=db)
        assert captured["resolved_filter"] is None
        assert captured["since_minutes"] is None
        assert captured["limit"] == 50

    async def test_db_error_fails_open(self):
        db = _mock_db_raises(RuntimeError("error_logs table missing"))
        result = await recent_errors(db=db)
        assert result["count"] == 0
        assert result["errors"] == []


# ---------------------------------------------------------------------------
# recent_jobs_costs
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestRecentJobsCosts:
    async def test_returns_jobs_with_costs(self):
        jid_a = uuid4()
        jid_b = uuid4()
        now = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
        db = _mock_db_rows([
            {
                "job_id": jid_a, "job_status": "completed",
                "job_created_at": now,
                "calls": 12, "cost_usd": 0.42, "prompt_tokens": 4000,
                "completion_tokens": 1500, "latency_ms": 22000,
            },
            {
                "job_id": jid_b, "job_status": "running",
                "job_created_at": now,
                "calls": 0, "cost_usd": 0, "prompt_tokens": 0,
                "completion_tokens": 0, "latency_ms": 0,
            },
        ])

        result = await recent_jobs_costs(window_minutes=1440, db=db)

        assert result["count"] == 2
        assert result["total_cost_usd"] == pytest.approx(0.42)
        # Zero-call job is preserved (LEFT JOIN); not silently dropped.
        zero = [j for j in result["jobs"] if j["job_id"] == str(jid_b)][0]
        assert zero["calls"] == 0
        assert zero["status"] == "running"

    async def test_db_error_fails_open(self):
        db = _mock_db_raises(RuntimeError("jobs missing"))
        result = await recent_jobs_costs(window_minutes=60, db=db)
        assert result == {
            "window_minutes": 60,
            "count": 0,
            "total_cost_usd": 0.0,
            "jobs": [],
        }


# ---------------------------------------------------------------------------
# Endpoints — TestClient with require_api_key bypassed.
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    app.dependency_overrides[require_api_key] = lambda: "test-key"
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.mark.smoke
class TestObservabilityEndpoints:
    def test_llm_rollup_endpoint_returns_payload(self, client):
        with patch("app.routers.observability.observability_rollups.llm_rollup",
                   new=AsyncMock(return_value={
                       "window_minutes": 60,
                       "total_calls": 5,
                       "total_cost_usd": 0.10,
                       "by_model": [{"provider": "openai", "model": "gpt-4o",
                                     "calls": 5, "successes": 5, "failures": 0,
                                     "cost_usd": 0.10, "prompt_tokens": 1000,
                                     "completion_tokens": 500,
                                     "latency_ms_sum": 12000,
                                     "latency_ms_p50": 2000, "latency_ms_p95": 3500,
                                     "latency_ms_p99": 4000}],
                   })):
            r = client.get("/observability/llm?window_minutes=60")
        assert r.status_code == 200
        body = r.json()
        assert body["total_calls"] == 5
        assert body["by_model"][0]["model"] == "gpt-4o"

    def test_llm_rollup_rejects_window_above_max(self, client):
        """7d cap enforced via Query(le=10080)."""
        r = client.get("/observability/llm?window_minutes=99999")
        assert r.status_code == 422

    def test_errors_endpoint_returns_payload(self, client):
        with patch("app.routers.observability.observability_rollups.recent_errors",
                   new=AsyncMock(return_value={
                       "filters": {"resolved": False, "since_minutes": None, "limit": 50},
                       "count": 0, "errors": [],
                   })):
            r = client.get("/observability/errors?resolved=false")
        assert r.status_code == 200
        body = r.json()
        assert body["filters"]["resolved"] is False
        assert body["count"] == 0

    def test_errors_endpoint_rejects_limit_above_cap(self, client):
        r = client.get("/observability/errors?limit=99999")
        assert r.status_code == 422

    def test_jobs_endpoint_returns_payload(self, client):
        with patch("app.routers.observability.observability_rollups.recent_jobs_costs",
                   new=AsyncMock(return_value={
                       "window_minutes": 1440, "count": 0,
                       "total_cost_usd": 0.0, "jobs": [],
                   })):
            r = client.get("/observability/jobs?window_minutes=60&limit=10")
        assert r.status_code == 200
        body = r.json()
        assert body["window_minutes"] == 1440  # value comes from helper, not query
