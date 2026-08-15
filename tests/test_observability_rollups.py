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
        # §17.284 — fail-open shape carries data_source="error".
        assert result == {
            "window_minutes": 60,
            "total_calls": 0,
            "total_cost_usd": 0.0,
            "by_model": [],
            "data_source": "error",
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
        # §17.284 — fail-open shape carries data_source="error".
        assert result == {
            "window_minutes": 60,
            "count": 0,
            "total_cost_usd": 0.0,
            "jobs": [],
            "data_source": "error",
        }


# ---------------------------------------------------------------------------
# §17.284 — data_source ("ok" | "error") contract across all three helpers.
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestDataSourceFlag:
    """§17.284 — distinguish a real empty rollup from a fail-open fallback.

    Pre-§17.284 the dashboards reading these endpoints couldn't tell the
    difference between "no LLM traffic in the window" and "the rollup
    query just blew up — the zeros are a placeholder." Each helper now
    carries ``data_source`` so dashboards can grey-out / warn / re-poll
    accordingly.
    """

    async def test_llm_rollup_empty_carries_ok_source(self):
        """No rows in the window (empty result set) → data_source="ok"."""
        db = _mock_db_rows([])
        result = await llm_rollup(window_minutes=60, db=db)
        assert result["by_model"] == []
        assert result["data_source"] == "ok"

    async def test_llm_rollup_populated_carries_ok_source(self):
        """Real rollup with rows → data_source="ok"."""
        db = _mock_db_rows([
            {"provider": "openai", "model": "gpt-4o", "calls": 3,
             "successes": 3, "failures": 0,
             "cost_usd": 0.04, "prompt_tokens": 6000,
             "completion_tokens": 2000, "latency_ms_sum": 25000,
             "latency_ms_p50": 8000, "latency_ms_p95": 12000,
             "latency_ms_p99": 13000},
        ])
        result = await llm_rollup(window_minutes=60, db=db)
        assert len(result["by_model"]) == 1
        assert result["data_source"] == "ok"

    async def test_llm_rollup_db_error_carries_error_source(self):
        """DB error → empty shape + data_source="error"."""
        db = _mock_db_raises(RuntimeError("table missing"))
        result = await llm_rollup(window_minutes=60, db=db)
        assert result["data_source"] == "error"

    async def test_recent_errors_empty_carries_ok_source(self):
        db = _mock_db_rows([])
        result = await recent_errors(db=db)
        assert result["count"] == 0
        assert result["data_source"] == "ok"

    async def test_recent_errors_db_error_carries_error_source(self):
        db = _mock_db_raises(RuntimeError("error_logs missing"))
        result = await recent_errors(db=db)
        assert result["data_source"] == "error"

    async def test_recent_jobs_costs_empty_carries_ok_source(self):
        db = _mock_db_rows([])
        result = await recent_jobs_costs(window_minutes=60, db=db)
        assert result["count"] == 0
        assert result["data_source"] == "ok"

    async def test_recent_jobs_costs_db_error_carries_error_source(self):
        db = _mock_db_raises(RuntimeError("jobs missing"))
        result = await recent_jobs_costs(window_minutes=60, db=db)
        assert result["data_source"] == "error"


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


# ---------------------------------------------------------------------------
# get_job_traces (§17.787)
# ---------------------------------------------------------------------------


def _trace_row(**over):
    """A minimal llm_traces mappings() row; override any field."""
    base = {
        "id": 1, "node_id": uuid4(), "call_kind": "synthesis",
        "request_kind": "chat", "provider": "openai", "model": "gpt-4o",
        "system_prompt": "you are helpful", "request_content": '[{"role":"user"}]',
        "response_content": "hi there", "tool_calls": None,
        "temperature": 0.7, "max_tokens": 512,
        "prompt_tokens": 100, "completion_tokens": 20,
        "latency_ms": 1234, "success": True, "error": None,
        "created_at": datetime(2026, 8, 15, tzinfo=timezone.utc),
    }
    base.update(over)
    return base


@pytest.mark.smoke
class TestGetJobTraces:
    async def test_maps_rows_in_call_order(self):
        from app.modules.observability_rollups import get_job_traces
        job_id = str(uuid4())
        db = _mock_db_rows([_trace_row(id=1), _trace_row(id=2, success=False,
                                                          error="boom")])
        with patch("app.config.settings") as s:
            s.trace_capture_enabled = True
            out = await get_job_traces(job_id=job_id, db=db)
        assert out["data_source"] == "ok"
        assert out["job_id"] == job_id
        assert out["count"] == 2
        assert out["capture_enabled"] is True
        assert [t["id"] for t in out["traces"]] == [1, 2]
        assert out["traces"][0]["temperature"] == 0.7
        assert out["traces"][1]["success"] is False
        assert out["traces"][1]["error"] == "boom"

    async def test_tool_calls_json_string_is_decoded(self):
        """A JSONB value handed back as a raw string is normalized to a list."""
        from app.modules.observability_rollups import get_job_traces
        db = _mock_db_rows([_trace_row(
            tool_calls='[{"id":"c1","name":"search","arguments":{"q":"x"}}]')])
        with patch("app.config.settings") as s:
            s.trace_capture_enabled = True
            out = await get_job_traces(job_id=str(uuid4()), db=db)
        tc = out["traces"][0]["tool_calls"]
        assert isinstance(tc, list) and tc[0]["name"] == "search"

    async def test_kind_filter_threads_to_query(self):
        from app.modules.observability_rollups import get_job_traces
        db = _mock_db_rows([])
        with patch("app.config.settings") as s:
            s.trace_capture_enabled = False
            await get_job_traces(job_id=str(uuid4()), kind="tool_call",
                                 limit=10, offset=5, db=db)
        _, kwargs = db.execute.call_args
        params = db.execute.call_args[0][1]
        assert params["kind_filter"] == "tool_call"
        assert params["limit"] == 10 and params["offset"] == 5

    async def test_capture_off_flag_surfaces_on_empty(self):
        from app.modules.observability_rollups import get_job_traces
        db = _mock_db_rows([])
        with patch("app.config.settings") as s:
            s.trace_capture_enabled = False
            out = await get_job_traces(job_id=str(uuid4()), db=db)
        assert out["count"] == 0
        assert out["capture_enabled"] is False

    async def test_fail_open_on_db_error(self):
        from app.modules.observability_rollups import get_job_traces
        db = _mock_db_raises(RuntimeError("no such table: llm_traces"))
        with patch("app.config.settings") as s:
            s.trace_capture_enabled = True
            out = await get_job_traces(job_id=str(uuid4()), db=db)
        assert out["data_source"] == "error"
        assert out["traces"] == []


@pytest.mark.smoke
class TestJobTracesEndpoint:
    def test_returns_payload(self, client):
        job_id = str(uuid4())
        with patch("app.routers.observability.observability_rollups.get_job_traces",
                   new=AsyncMock(return_value={
                       "job_id": job_id, "count": 1, "limit": 50, "offset": 0,
                       "capture_enabled": True,
                       "traces": [{"id": 1, "request_kind": "chat",
                                   "provider": "openai", "model": "gpt-4o",
                                   "response_content": "hi"}],
                       "data_source": "ok",
                   })):
            r = client.get(f"/trace/{job_id}?kind=chat&limit=50")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        assert body["traces"][0]["response_content"] == "hi"

    def test_rejects_non_uuid(self, client):
        r = client.get("/trace/not-a-uuid")
        assert r.status_code == 422

    def test_rejects_limit_above_cap(self, client):
        r = client.get(f"/trace/{uuid4()}?limit=99999")
        assert r.status_code == 422
