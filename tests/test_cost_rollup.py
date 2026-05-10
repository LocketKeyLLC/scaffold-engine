"""Sprint J.3.b — cost rollup helpers + endpoint + /exec/status extension.

Coverage:
  - get_job_cost_totals: returns dict with summed cost/tokens/latency;
    fail-open shape on DB error or missing rows
  - get_job_costs: includes by-provider breakdown sorted desc by cost
  - GET /jobs/{id}/costs endpoint: 200 with payload, 422 on bad UUID
  - /exec/status response includes the new `costs` block
  - SDK client.jobs.costs(...) hits the right endpoint
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.auth import require_api_key
from app.main import app
from app.modules.cost_rollup import get_job_cost_totals, get_job_costs


def _mock_db(
    totals_row: dict | None,
    breakdown_rows: list[dict] | None = None,
    kind_rows: list[dict] | None = None,
):
    """Build an AsyncMock db whose execute returns totals (first),
    by-provider breakdown (second), and §17.90 by-kind breakdown (third).
    """
    totals_result = MagicMock()
    totals_mappings = MagicMock()
    totals_mappings.first.return_value = totals_row
    totals_result.mappings.return_value = totals_mappings

    breakdown_result = MagicMock()
    breakdown_mappings = MagicMock()
    breakdown_mappings.all.return_value = breakdown_rows or []
    breakdown_result.mappings.return_value = breakdown_mappings

    kind_result = MagicMock()
    kind_mappings = MagicMock()
    kind_mappings.all.return_value = kind_rows or []
    kind_result.mappings.return_value = kind_mappings

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        totals_result, breakdown_result, kind_result,
    ])
    return db


@pytest.mark.smoke
class TestGetJobCostTotals:
    """Single SUM query — used by /exec/status (lightweight)."""

    async def test_returns_summed_totals(self):
        totals = {
            "total_cost_usd": 0.0123,
            "total_prompt_tokens": 5000,
            "total_completion_tokens": 2000,
            "total_latency_ms": 45000,
            "call_count": 23,
        }
        db = _mock_db(totals)
        result = await get_job_cost_totals("job-1", db)
        assert result["total_cost_usd"] == pytest.approx(0.0123)
        assert result["total_prompt_tokens"] == 5000
        assert result["total_completion_tokens"] == 2000
        assert result["total_latency_ms"] == 45000
        assert result["call_count"] == 23

    async def test_no_calls_returns_zero_shape(self):
        """Job with no logged LLM calls returns the zero shape, not None."""
        # COALESCE in the SQL means COUNT(*)=0 still produces a row.
        db = _mock_db({
            "total_cost_usd": 0,
            "total_prompt_tokens": 0,
            "total_completion_tokens": 0,
            "total_latency_ms": 0,
            "call_count": 0,
        })
        result = await get_job_cost_totals("job-empty", db)
        assert result["total_cost_usd"] == 0.0
        assert result["call_count"] == 0

    async def test_db_error_fails_open(self):
        """Missing llm_call_logs table or transient DB failure → zero shape,
        not 500. /exec/status's hot path can't tolerate telemetry breakage."""
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=RuntimeError("relation does not exist"))
        result = await get_job_cost_totals("job-1", db)
        assert result == {
            "total_cost_usd": 0.0,
            "total_prompt_tokens": 0,
            "total_completion_tokens": 0,
            "total_latency_ms": 0,
            "call_count": 0,
        }


@pytest.mark.smoke
class TestGetJobCosts:
    """Totals + per-(provider, model) breakdown — used by /jobs/{id}/costs."""

    async def test_returns_breakdown_in_response(self):
        totals = {
            "total_cost_usd": 0.05,
            "total_prompt_tokens": 10000,
            "total_completion_tokens": 4000,
            "total_latency_ms": 60000,
            "call_count": 10,
        }
        breakdown = [
            {"provider": "openai", "model": "gpt-4o", "calls": 3,
             "cost_usd": 0.04, "prompt_tokens": 6000,
             "completion_tokens": 2000, "latency_ms": 25000},
            {"provider": "ollama", "model": "qwen3:4b", "calls": 7,
             "cost_usd": 0.0, "prompt_tokens": 4000,
             "completion_tokens": 2000, "latency_ms": 35000},
        ]
        db = _mock_db(totals, breakdown)
        result = await get_job_costs("job-1", db)

        assert result["job_id"] == "job-1"
        assert result["total_cost_usd"] == pytest.approx(0.05)
        assert result["call_count"] == 10
        assert len(result["by_provider"]) == 2
        # Breakdown rows preserve order (SQL sorts descending by cost).
        assert result["by_provider"][0]["provider"] == "openai"
        assert result["by_provider"][0]["cost_usd"] == pytest.approx(0.04)
        assert result["by_provider"][1]["provider"] == "ollama"
        assert result["by_provider"][1]["cost_usd"] == 0.0

    async def test_no_breakdown_returns_empty_list(self):
        totals = {
            "total_cost_usd": 0, "total_prompt_tokens": 0,
            "total_completion_tokens": 0, "total_latency_ms": 0,
            "call_count": 0,
        }
        db = _mock_db(totals, [])
        result = await get_job_costs("job-empty", db)
        assert result["by_provider"] == []
        assert result["by_kind"] == []  # §17.90 — empty kind list too
        assert result["total_cost_usd"] == 0.0


@pytest.mark.smoke
class TestGetJobCostsKindBreakdown:
    """§17.90 — by_kind breakdown splits synthesis spend from execution spend."""

    async def test_returns_kind_breakdown_when_present(self):
        totals = {
            "total_cost_usd": 0.06,
            "total_prompt_tokens": 11000,
            "total_completion_tokens": 4500,
            "total_latency_ms": 65000,
            "call_count": 11,
        }
        kind_rows = [
            # SQL coalesces NULL → 'uncategorized'; here the rows
            # already carry the resolved literal.
            {"kind": "synthesis", "calls": 1,
             "cost_usd": 0.012, "prompt_tokens": 3000,
             "completion_tokens": 800, "latency_ms": 7000},
            {"kind": "uncategorized", "calls": 10,
             "cost_usd": 0.048, "prompt_tokens": 8000,
             "completion_tokens": 3700, "latency_ms": 58000},
        ]
        db = _mock_db(totals, [], kind_rows)
        result = await get_job_costs("job-1", db)

        assert len(result["by_kind"]) == 2
        # SQL sorts descending by cost_usd; synthesis is first only if it
        # spent more than execution. In this fixture it did not, so the
        # ordering reflects the input list (preserved as-is by the helper).
        kinds = {row["kind"]: row for row in result["by_kind"]}
        assert kinds["synthesis"]["calls"] == 1
        assert kinds["synthesis"]["cost_usd"] == pytest.approx(0.012)
        assert kinds["uncategorized"]["calls"] == 10
        assert kinds["uncategorized"]["cost_usd"] == pytest.approx(0.048)

    async def test_kind_breakdown_fails_open_on_db_error(self):
        """If the third (kind) query raises — e.g. test env without the
        §17.90 migration — by_kind comes back empty but by_provider and
        totals are still populated. Fail-open posture matches the rest
        of the rollup."""
        totals = {
            "total_cost_usd": 0.05,
            "total_prompt_tokens": 10000,
            "total_completion_tokens": 4000,
            "total_latency_ms": 60000,
            "call_count": 10,
        }
        breakdown = [
            {"provider": "openai", "model": "gpt-4o", "calls": 10,
             "cost_usd": 0.05, "prompt_tokens": 10000,
             "completion_tokens": 4000, "latency_ms": 60000},
        ]
        # Build a db that succeeds on the first two queries then raises.
        totals_result = MagicMock()
        totals_mappings = MagicMock()
        totals_mappings.first.return_value = totals
        totals_result.mappings.return_value = totals_mappings
        breakdown_result = MagicMock()
        breakdown_mappings = MagicMock()
        breakdown_mappings.all.return_value = breakdown
        breakdown_result.mappings.return_value = breakdown_mappings

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            totals_result,
            breakdown_result,
            RuntimeError("column llm_call_logs.call_kind does not exist"),
        ])
        result = await get_job_costs("job-1", db)
        assert result["by_kind"] == []
        # Totals and by_provider remain populated — fail-open is partial.
        assert result["total_cost_usd"] == pytest.approx(0.05)
        assert len(result["by_provider"]) == 1


# ---------------------------------------------------------------------------
# Endpoint integration via TestClient
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    app.dependency_overrides[require_api_key] = lambda: "test"
    try:
        with TestClient(app) as tc:
            yield tc
    finally:
        app.dependency_overrides.pop(require_api_key, None)


@pytest.mark.smoke
class TestCostsEndpoint:
    """GET /jobs/{job_id}/costs returns the response model."""

    def test_returns_payload(self, client):
        canned = {
            "job_id": "11111111-1111-1111-1111-111111111111",
            "total_cost_usd": 0.001234,
            "total_prompt_tokens": 1500,
            "total_completion_tokens": 600,
            "total_latency_ms": 12000,
            "call_count": 4,
            "by_provider": [
                {"provider": "openai", "model": "gpt-4o-mini", "calls": 4,
                 "cost_usd": 0.001234, "prompt_tokens": 1500,
                 "completion_tokens": 600, "latency_ms": 12000},
            ],
        }
        with patch(
            "app.modules.cost_rollup.get_job_costs",
            new=AsyncMock(return_value=canned),
        ):
            resp = client.get(f"/jobs/{canned['job_id']}/costs")
        assert resp.status_code == 200
        body = resp.json()
        assert body["job_id"] == canned["job_id"]
        assert body["total_cost_usd"] == pytest.approx(0.001234)
        assert body["call_count"] == 4
        assert body["by_provider"][0]["provider"] == "openai"
        assert body["by_provider"][0]["model"] == "gpt-4o-mini"

    def test_rejects_invalid_uuid(self, client):
        resp = client.get("/jobs/not-a-uuid/costs")
        assert resp.status_code == 422

    def test_zero_shape_for_unknown_job(self, client):
        """A job_id that's a valid UUID but has no logged calls returns
        the zero shape (200, not 404). Matches the fail-open posture
        — operators can still query a freshly-created job."""
        canned = {
            "job_id": "22222222-2222-2222-2222-222222222222",
            "total_cost_usd": 0.0,
            "total_prompt_tokens": 0,
            "total_completion_tokens": 0,
            "total_latency_ms": 0,
            "call_count": 0,
            "by_provider": [],
        }
        with patch(
            "app.modules.cost_rollup.get_job_costs",
            new=AsyncMock(return_value=canned),
        ):
            resp = client.get(f"/jobs/{canned['job_id']}/costs")
        assert resp.status_code == 200
        body = resp.json()
        assert body["call_count"] == 0
        assert body["by_provider"] == []


# ---------------------------------------------------------------------------
# /exec/status extension
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestExecStatusCostsBlock:
    """execution_status now returns a `costs` block alongside the existing
    fields. Lightweight summary; no breakdown."""

    async def test_status_includes_costs_totals(self):
        from app.modules.execution_handler import execution_status

        job_row = SimpleNamespace(
            id="j1", title="x", status="completed",
            compiled_output="output",
            compiled_output_synthesized=False,
            compile_synthesis_override=None,
        )
        job_result = MagicMock()
        job_result.fetchone.return_value = job_row
        nodes_result = MagicMock()
        nodes_result.fetchall.return_value = []

        # The cost-totals SUM query lands as the third execute call.
        cost_totals_row = {
            "total_cost_usd": 0.025, "total_prompt_tokens": 5000,
            "total_completion_tokens": 2000, "total_latency_ms": 30000,
            "call_count": 12,
        }
        cost_result = MagicMock()
        cost_mappings = MagicMock()
        cost_mappings.first.return_value = cost_totals_row
        cost_result.mappings.return_value = cost_mappings

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            job_result, nodes_result, cost_result,
        ])

        result = await execution_status(uuid4(), db)
        assert "costs" in result
        costs = result["costs"]
        assert costs["total_cost_usd"] == pytest.approx(0.025)
        assert costs["call_count"] == 12
        assert costs["total_latency_ms"] == 30000

    async def test_status_costs_zero_shape_when_no_calls_logged(self):
        """A job with no LLM calls yet still gets the costs block,
        zero-valued. /exec/status callers can render unconditionally."""
        from app.modules.execution_handler import execution_status

        job_row = SimpleNamespace(
            id="j1", title="x", status="planning",
            compiled_output=None,
            compiled_output_synthesized=False,
            compile_synthesis_override=None,
        )
        job_result = MagicMock()
        job_result.fetchone.return_value = job_row
        nodes_result = MagicMock()
        nodes_result.fetchall.return_value = []
        cost_result = MagicMock()
        cost_mappings = MagicMock()
        cost_mappings.first.return_value = {
            "total_cost_usd": 0, "total_prompt_tokens": 0,
            "total_completion_tokens": 0, "total_latency_ms": 0,
            "call_count": 0,
        }
        cost_result.mappings.return_value = cost_mappings

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            job_result, nodes_result, cost_result,
        ])

        result = await execution_status(uuid4(), db)
        assert result["costs"]["call_count"] == 0
        assert result["costs"]["total_cost_usd"] == 0.0


# ---------------------------------------------------------------------------
# SDK
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestSdkCostsMethod:
    """SDK client.jobs.costs(job_id) hits the right endpoint."""

    def test_sync_costs_calls_get(self):
        from scaffold_client._resources import JobsResource

        client = MagicMock()
        client.request.return_value = {"job_id": "j1", "call_count": 0}
        jobs = JobsResource(client)
        result = jobs.costs("j1")
        client.request.assert_called_once_with("GET", "/jobs/j1/costs")
        assert result["job_id"] == "j1"
