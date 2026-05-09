"""Sprint X.26 — push X.20 rollup threshold eval."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.observability import thresholds as _thresholds


def _mock_db(*, unresolved: int = 0):
    """Return a db whose dedup probes always miss (no cooldown) and
    whose unresolved-error count returns ``unresolved``."""

    async def _execute(sql, params=None):
        sql_text = str(sql)
        result = MagicMock()
        if "FROM error_logs" in sql_text and "resolved = FALSE" in sql_text:
            result.scalar.return_value = unresolved
            return result
        # dedup probe → no cooldown so emits proceed
        if "FROM system_alerts" in sql_text and "WHERE dedup_key" in sql_text:
            result.first.return_value = None
            return result
        # INSERT INTO system_alerts → return a fake id
        if "INSERT INTO system_alerts" in sql_text:
            result.scalar.return_value = "alert-id"
            return result
        # default
        return result

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_execute)
    db.commit = AsyncMock()
    return db


@pytest.mark.smoke
class TestEvaluateThresholds:
    async def test_unresolved_errors_breach_emits_alert(self, monkeypatch):
        monkeypatch.setattr(
            "app.observability.thresholds.settings.alert_unresolved_errors_threshold",
            1, raising=False,
        )
        monkeypatch.setattr(
            "app.observability.thresholds.settings.alert_eval_window_minutes", 60,
            raising=False,
        )
        # No cost / latency breach in this scenario.
        rollup = {"total_cost_usd": 0.0, "by_model": []}
        db = _mock_db(unresolved=3)
        with patch(
            "app.observability.thresholds.observability_rollups.llm_rollup",
            new=AsyncMock(return_value=rollup),
        ):
            summary = await _thresholds.evaluate_thresholds(db)
        assert summary["unresolved_errors"] == 3
        assert any(k == "oncall.errors_unresolved" for k, _ in summary["fired"])

    async def test_no_breach_no_alerts(self, monkeypatch):
        monkeypatch.setattr(
            "app.observability.thresholds.settings.alert_unresolved_errors_threshold",
            10, raising=False,
        )
        monkeypatch.setattr(
            "app.observability.thresholds.settings.alert_cost_window_usd_threshold",
            100.0, raising=False,
        )
        monkeypatch.setattr(
            "app.observability.thresholds.settings.alert_p95_latency_ms_threshold",
            999_999, raising=False,
        )
        db = _mock_db(unresolved=2)
        with patch(
            "app.observability.thresholds.observability_rollups.llm_rollup",
            new=AsyncMock(return_value={
                "total_cost_usd": 0.10,
                "by_model": [{"provider": "ollama", "model": "qwen3:4b",
                              "latency_ms_p95": 500}],
            }),
        ):
            summary = await _thresholds.evaluate_thresholds(db)
        assert summary["fired"] == []

    async def test_cost_window_breach_fires(self, monkeypatch):
        monkeypatch.setattr(
            "app.observability.thresholds.settings.alert_unresolved_errors_threshold",
            999_999, raising=False,
        )
        monkeypatch.setattr(
            "app.observability.thresholds.settings.alert_cost_window_usd_threshold",
            5.0, raising=False,
        )
        monkeypatch.setattr(
            "app.observability.thresholds.settings.alert_p95_latency_ms_threshold",
            999_999, raising=False,
        )
        db = _mock_db(unresolved=0)
        with patch(
            "app.observability.thresholds.observability_rollups.llm_rollup",
            new=AsyncMock(return_value={
                "total_cost_usd": 6.50,
                "by_model": [],
            }),
        ):
            summary = await _thresholds.evaluate_thresholds(db)
        assert any(k == "cost.window_exceeded" for k, _ in summary["fired"])

    async def test_p95_latency_breach_fires_per_model(self, monkeypatch):
        monkeypatch.setattr(
            "app.observability.thresholds.settings.alert_unresolved_errors_threshold",
            999_999, raising=False,
        )
        monkeypatch.setattr(
            "app.observability.thresholds.settings.alert_cost_window_usd_threshold",
            999_999.0, raising=False,
        )
        monkeypatch.setattr(
            "app.observability.thresholds.settings.alert_p95_latency_ms_threshold",
            120_000, raising=False,
        )
        db = _mock_db(unresolved=0)
        with patch(
            "app.observability.thresholds.observability_rollups.llm_rollup",
            new=AsyncMock(return_value={
                "total_cost_usd": 0.0,
                "by_model": [
                    {"provider": "openai", "model": "gpt-4o", "latency_ms_p95": 130_000},
                    {"provider": "ollama", "model": "qwen3:4b", "latency_ms_p95": 1500},
                ],
            }),
        ):
            summary = await _thresholds.evaluate_thresholds(db)
        keys = [k for k, _ in summary["fired"]]
        assert any("latency.p95_exceeded:openai:gpt-4o" in k for k in keys)
        assert not any("ollama:qwen3" in k for k in keys)
