"""§17.777 — per-job cost/token budget enforcement.

Coverage:
  - resolve_job_budget: per-job override wins over settings default;
    None inherits the default; 0 = unlimited on that axis.
  - get_budget_status: under/over each axis, unlimited axes, fail-open
    (totals data_source == "error" never reports a breach).
  - enforce_job_budget: valve off → no-op; under budget → None; over the
    token cap → flips job to 'failed' + returns terminal dict; over the USD
    cap → limit == "cost"; fail-open telemetry read never kills the job.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.modules import cost_budget
from app.modules.cost_budget import (
    COST_BUDGET_SUMMARY,
    enforce_job_budget,
    get_budget_status,
    resolve_job_budget,
)

JOB_ID = "11111111-1111-1111-1111-111111111111"


def _mapping_result(row):
    """A db.execute() result whose .mappings().first() returns ``row``."""
    res = MagicMock()
    mappings = MagicMock()
    mappings.first.return_value = row
    res.mappings.return_value = mappings
    return res


def _first_result(row):
    """A db.execute() result whose .first() returns ``row`` (UPDATE ... RETURNING)."""
    res = MagicMock()
    res.first.return_value = row
    return res


def _mock_db(*, budget_row, totals_row, update_row=("id",)):
    """AsyncMock db feeding, in order:
      1. _load_job_budget       → .mappings().first() = budget_row
      2. get_job_cost_totals    → .mappings().first() = totals_row
      3. (enforce) UPDATE       → .first() = update_row
    """
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _mapping_result(budget_row),
        _mapping_result(totals_row),
        _first_result(update_row),
    ])
    db.commit = AsyncMock()
    return db


def _totals(prompt=0, completion=0, cost=0.0, data_source="ok"):
    return {
        "total_cost_usd": cost,
        "total_prompt_tokens": prompt,
        "total_completion_tokens": completion,
        "total_latency_ms": 0,
        "call_count": 0,
        "data_source": data_source,
    }


@pytest.mark.smoke
class TestResolveJobBudget:
    def test_override_wins_over_default(self, monkeypatch):
        monkeypatch.setattr(cost_budget.settings, "cost_budget_default_max_tokens", 100)
        monkeypatch.setattr(cost_budget.settings, "cost_budget_default_max_usd", 1.0)
        assert resolve_job_budget(500, 2.5) == (500, 2.5)

    def test_none_inherits_default(self, monkeypatch):
        monkeypatch.setattr(cost_budget.settings, "cost_budget_default_max_tokens", 100)
        monkeypatch.setattr(cost_budget.settings, "cost_budget_default_max_usd", 1.0)
        assert resolve_job_budget(None, None) == (100, 1.0)

    def test_explicit_zero_override_is_unlimited(self, monkeypatch):
        monkeypatch.setattr(cost_budget.settings, "cost_budget_default_max_tokens", 100)
        monkeypatch.setattr(cost_budget.settings, "cost_budget_default_max_usd", 1.0)
        # 0 override beats a non-zero default → unlimited on that axis.
        assert resolve_job_budget(0, 0.0) == (0, 0.0)


@pytest.mark.smoke
class TestGetBudgetStatus:
    async def test_under_token_budget_not_exceeded(self, monkeypatch):
        monkeypatch.setattr(cost_budget.settings, "cost_budget_enforcement_enabled", True)
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            _mapping_result({"token_budget": 1000, "cost_budget_usd": None}),
            _mapping_result(_totals(prompt=100, completion=100)),
        ])
        status = await get_budget_status(JOB_ID, db)
        assert status.spent_tokens == 200
        assert status.max_tokens == 1000
        assert status.tokens_remaining == 800
        assert status.exceeded is False
        assert status.limit is None

    async def test_over_token_budget_flags_tokens(self, monkeypatch):
        monkeypatch.setattr(cost_budget.settings, "cost_budget_enforcement_enabled", True)
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            _mapping_result({"token_budget": 150, "cost_budget_usd": None}),
            _mapping_result(_totals(prompt=100, completion=100)),
        ])
        status = await get_budget_status(JOB_ID, db)
        assert status.exceeded is True
        assert status.limit == "tokens"
        assert status.tokens_remaining == 0

    async def test_over_cost_budget_flags_cost(self, monkeypatch):
        monkeypatch.setattr(cost_budget.settings, "cost_budget_enforcement_enabled", True)
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            _mapping_result({"token_budget": 0, "cost_budget_usd": 1.0}),  # tokens unlimited
            _mapping_result(_totals(cost=2.5)),
        ])
        status = await get_budget_status(JOB_ID, db)
        assert status.exceeded is True
        assert status.limit == "cost"

    async def test_failopen_read_never_reports_breach(self, monkeypatch):
        monkeypatch.setattr(cost_budget.settings, "cost_budget_enforcement_enabled", True)
        db = AsyncMock()
        # The totals query RAISES → get_job_cost_totals fails open to a zero
        # shape tagged data_source="error"; the budget read must not report a
        # breach off numbers it couldn't trust.
        db.execute = AsyncMock(side_effect=[
            _mapping_result({"token_budget": 10, "cost_budget_usd": None}),
            RuntimeError("db down"),
        ])
        status = await get_budget_status(JOB_ID, db)
        # Spend is way over the cap, but the telemetry read failed → no breach.
        assert status.data_source == "error"
        assert status.exceeded is False
        assert status.limit is None


@pytest.mark.smoke
class TestEnforceJobBudget:
    async def test_valve_off_is_noop(self, monkeypatch):
        monkeypatch.setattr(cost_budget.settings, "cost_budget_enforcement_enabled", False)
        db = AsyncMock()
        db.execute = AsyncMock()
        result = await enforce_job_budget(db, JOB_ID)
        assert result is None
        db.execute.assert_not_called()  # short-circuits before any query

    async def test_under_budget_returns_none(self, monkeypatch):
        monkeypatch.setattr(cost_budget.settings, "cost_budget_enforcement_enabled", True)
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            _mapping_result({"token_budget": 1000, "cost_budget_usd": None}),
            _mapping_result(_totals(prompt=100, completion=100)),
        ])
        assert await enforce_job_budget(db, JOB_ID) is None

    async def test_over_token_budget_hard_stops(self, monkeypatch):
        monkeypatch.setattr(cost_budget.settings, "cost_budget_enforcement_enabled", True)
        db = _mock_db(
            budget_row={"token_budget": 150, "cost_budget_usd": None},
            totals_row=_totals(prompt=100, completion=100),
        )
        result = await enforce_job_budget(db, JOB_ID)
        assert result is not None
        assert result["status"] == "budget_exhausted"
        assert result["reason"] == COST_BUDGET_SUMMARY
        assert result["limit"] == "tokens"
        assert result["spent_tokens"] == 200
        # The failing UPDATE ran and the write was committed.
        assert db.commit.await_count == 1
        update_sql = db.execute.await_args_list[-1].args[0].text
        assert "status = 'failed'" in update_sql
        assert COST_BUDGET_SUMMARY == db.execute.await_args_list[-1].args[1]["summary"]

    async def test_over_cost_budget_hard_stops(self, monkeypatch):
        monkeypatch.setattr(cost_budget.settings, "cost_budget_enforcement_enabled", True)
        db = _mock_db(
            budget_row={"token_budget": 0, "cost_budget_usd": 1.0},
            totals_row=_totals(cost=5.0),
        )
        result = await enforce_job_budget(db, JOB_ID)
        assert result is not None
        assert result["limit"] == "cost"

    async def test_failopen_read_does_not_stop_job(self, monkeypatch):
        monkeypatch.setattr(cost_budget.settings, "cost_budget_enforcement_enabled", True)
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            _mapping_result({"token_budget": 10, "cost_budget_usd": None}),
            RuntimeError("db down"),  # totals query raises → fail-open
        ])
        db.commit = AsyncMock()
        # Even though spend >> cap, a failed telemetry read must not kill the job.
        assert await enforce_job_budget(db, JOB_ID) is None
        db.commit.assert_not_called()

    async def test_no_cap_set_is_noop(self, monkeypatch):
        monkeypatch.setattr(cost_budget.settings, "cost_budget_enforcement_enabled", True)
        monkeypatch.setattr(cost_budget.settings, "cost_budget_default_max_tokens", 0)
        monkeypatch.setattr(cost_budget.settings, "cost_budget_default_max_usd", 0.0)
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            _mapping_result({"token_budget": None, "cost_budget_usd": None}),
            _mapping_result(_totals(prompt=10**9)),
        ])
        # Both axes unlimited (0) → nothing to enforce even with huge spend.
        assert await enforce_job_budget(db, JOB_ID) is None
