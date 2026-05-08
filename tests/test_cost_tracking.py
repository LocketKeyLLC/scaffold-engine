"""Sprint J.3.a — cost + latency telemetry foundation tests.

Coverage:
  - compute_cost_usd: priced provider/model returns USD; missing pricing
    falls through to 0; Ollama is always 0 by absence of seed rows;
    negative/zero token counts return 0.
  - record_llm_call: writes one llm_call_logs row with the right shape;
    reads ContextVars for job/node tagging; swallows DB failures so
    telemetry can never break the LLM call path.
  - ContextVars default to None; explicit set/reset propagate through.
"""
from __future__ import annotations

from contextvars import copy_context
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.utils.cost_tracking import (
    compute_cost_usd,
    current_job_id,
    current_node_id,
    record_llm_call,
)


def _mock_db_with_rate(input_rate: float, output_rate: float):
    """Build an AsyncMock db whose model_costs SELECT returns the given
    rates; INSERT into llm_call_logs is mocked separately so callers
    can assert it was/wasn't called."""
    rate_row = MagicMock()
    rate_row.first.return_value = (input_rate, output_rate)

    db = AsyncMock()
    db.execute = AsyncMock(return_value=rate_row)
    db.commit = AsyncMock()
    return db


def _mock_db_no_rate():
    """Build an AsyncMock whose SELECT returns no row (unknown
    provider/model — falls through to 0)."""
    rate_row = MagicMock()
    rate_row.first.return_value = None
    db = AsyncMock()
    db.execute = AsyncMock(return_value=rate_row)
    db.commit = AsyncMock()
    return db


@pytest.mark.smoke
class TestComputeCostUsd:
    """Cost formula: (prompt × in_rate + completion × out_rate) / 1M."""

    async def test_priced_call_returns_correct_usd(self):
        # gpt-4o-mini rates: input=0.15, output=0.60 per 1M
        # 1000 prompt + 500 completion → 0.15*1000/1M + 0.60*500/1M
        # = 0.00015 + 0.0003 = 0.00045
        db = _mock_db_with_rate(0.15, 0.60)
        cost = await compute_cost_usd(db, "openai", "gpt-4o-mini", 1000, 500)
        assert cost == pytest.approx(0.00045)

    async def test_unknown_provider_returns_zero(self):
        db = _mock_db_no_rate()
        cost = await compute_cost_usd(db, "ollama", "qwen3:4b", 5000, 2000)
        assert cost == 0.0

    async def test_zero_tokens_returns_zero(self):
        db = _mock_db_with_rate(2.50, 10.00)
        cost = await compute_cost_usd(db, "openai", "gpt-4o", 0, 0)
        assert cost == 0.0
        # No rate lookup needed when tokens are zero — short-circuit.
        # (Implementation may or may not skip the DB call; the contract
        # is just that the result is 0.)

    async def test_negative_tokens_clamped_to_zero(self):
        """Some providers return -1 / None for tokens on failure paths.
        Defensive: don't multiply by negatives."""
        db = _mock_db_with_rate(2.50, 10.00)
        cost = await compute_cost_usd(db, "openai", "gpt-4o", -100, 50)
        # Only the (clamped 0) prompt × 2.50 + 50 × 10.00 / 1M = 0.0005
        assert cost == pytest.approx(0.0005)

    async def test_blank_provider_or_model_returns_zero(self):
        db = _mock_db_no_rate()
        assert await compute_cost_usd(db, "", "model", 100, 100) == 0.0
        assert await compute_cost_usd(db, "provider", "", 100, 100) == 0.0


@pytest.mark.smoke
class TestRecordLlmCall:
    """record_llm_call writes a row + reads ContextVars for tagging."""

    async def test_writes_row_with_context_vars(self):
        captured_params = {}

        class _FakeDB:
            async def execute(self, sql, params=None):
                # First call: SELECT model_costs (return None)
                # Second call: INSERT llm_call_logs (capture params)
                if "INSERT INTO llm_call_logs" in str(sql):
                    captured_params.update(params or {})
                    return MagicMock()
                # SELECT model_costs
                row = MagicMock()
                row.first.return_value = None
                return row

            async def commit(self):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        # Set ContextVars within a copied context so we don't leak.
        ctx = copy_context()

        async def _runner():
            current_job_id.set("job-abc")
            current_node_id.set("node-xyz")
            resp = SimpleNamespace(
                provider="ollama", model="qwen3:4b",
                tokens_prompt=500, tokens_completion=200,
                total_duration_ms=1200, success=True,
            )
            with patch("app.database.async_session", lambda: _FakeDB()):
                await record_llm_call(resp)

        await ctx.run(_runner)

        assert captured_params.get("job_id") == "job-abc"
        assert captured_params.get("node_id") == "node-xyz"
        assert captured_params.get("provider") == "ollama"
        assert captured_params.get("model") == "qwen3:4b"
        assert captured_params.get("prompt_tokens") == 500
        assert captured_params.get("completion_tokens") == 200
        assert captured_params.get("latency_ms") == 1200
        assert captured_params.get("cost_usd") == 0.0  # ollama → no rate row
        assert captured_params.get("success") is True

    async def test_db_failure_swallowed(self):
        """If the DB write itself raises, record_llm_call must NOT
        propagate the exception — telemetry never breaks the LLM call."""
        class _BrokenDB:
            async def execute(self, *a, **kw):
                raise RuntimeError("connection refused")

            async def commit(self):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        resp = SimpleNamespace(
            provider="openai", model="gpt-4o",
            tokens_prompt=100, tokens_completion=50,
            total_duration_ms=200, success=True,
        )
        with patch("app.database.async_session", lambda: _BrokenDB()):
            # Must NOT raise.
            await record_llm_call(resp)

    async def test_no_context_vars_writes_null_job_id(self):
        captured = {}

        class _FakeDB:
            async def execute(self, sql, params=None):
                if "INSERT INTO llm_call_logs" in str(sql):
                    captured.update(params or {})
                    return MagicMock()
                row = MagicMock()
                row.first.return_value = None
                return row

            async def commit(self):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        # Fresh context — ContextVars default to None.
        ctx = copy_context()

        async def _runner():
            resp = SimpleNamespace(
                provider="ollama", model="qwen3:4b",
                tokens_prompt=10, tokens_completion=5,
                total_duration_ms=50, success=True,
            )
            with patch("app.database.async_session", lambda: _FakeDB()):
                await record_llm_call(resp)

        await ctx.run(_runner)

        # Off-job calls (validate_models, /optimize standalone) get
        # NULL job_id — still tracked, just ungrouped.
        assert captured.get("job_id") is None
        assert captured.get("node_id") is None


@pytest.mark.smoke
class TestContextVarDefaults:
    """Sanity: ContextVars default to None until explicitly set."""

    def test_defaults_are_none(self):
        # Run inside a fresh context so test order doesn't pollute.
        ctx = copy_context()
        assert ctx.run(current_job_id.get) is None
        assert ctx.run(current_node_id.get) is None

    def test_set_and_get_in_same_context(self):
        ctx = copy_context()

        def _set_and_read():
            current_job_id.set("ctx-test-job")
            return current_job_id.get()

        result = ctx.run(_set_and_read)
        assert result == "ctx-test-job"
        # Original context unaffected — copy_context isolates the change.
        assert current_job_id.get() is None
