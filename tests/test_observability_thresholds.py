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


# ---------------------------------------------------------------------------
# §17.192 — extended coverage for thresholds.refresh_gauges
# ---------------------------------------------------------------------------
#
# refresh_gauges() pushes 3 snapshot families to Prometheus on every eval
# tick (jobs_by_status / research_sessions_running / unresolved_errors_window).
# Pre-§17.192 these weren't directly covered. The tests below mock the db
# and assert (a) the gauges are updated with the queried values, (b) a
# previously-non-empty status that empties out is cleared (not stuck at
# the last value), (c) one query failing doesn't break the other two
# (fail-open guarantee).

def _mock_gauge_db(*, jobs_rows, sessions_running, unresolved_window,
                   fail_on_jobs=False, fail_on_sessions=False,
                   fail_on_errors=False):
    """Build a db whose execute routes to the right canned response based
    on the SQL substring — refresh_gauges runs 3 distinct queries."""

    async def _execute(sql, params=None):
        sql_text = str(sql)
        result = MagicMock()
        if "FROM jobs" in sql_text and "GROUP BY status" in sql_text:
            if fail_on_jobs:
                raise RuntimeError("simulated jobs query failure")
            mappings = MagicMock()
            mappings.all.return_value = jobs_rows
            result.mappings.return_value = mappings
            return result
        if "FROM research_sessions" in sql_text and "running" in sql_text:
            if fail_on_sessions:
                raise RuntimeError("simulated sessions query failure")
            result.scalar.return_value = sessions_running
            return result
        if "FROM error_logs" in sql_text and "resolved = FALSE" in sql_text:
            if fail_on_errors:
                raise RuntimeError("simulated errors query failure")
            result.scalar.return_value = unresolved_window
            return result
        return result

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_execute)
    return db


@pytest.mark.smoke
class TestRefreshGauges:
    @pytest.fixture(autouse=True)
    def _reset_metrics(self):
        from app.observability import metrics as _metrics
        _metrics.reset_for_tests()
        yield
        _metrics.reset_for_tests()

    async def test_jobs_by_status_gauge_populated_per_status(self):
        from app.observability import metrics as _metrics
        db = _mock_gauge_db(
            jobs_rows=[
                {"status": "executing", "c": 3},
                {"status": "completed", "c": 42},
                {"status": "failed", "c": 1},
            ],
            sessions_running=0, unresolved_window=0,
        )
        await _thresholds.refresh_gauges(db)
        # Read back via the registry — verify all 3 labels populated.
        assert _metrics.jobs_by_status.labels(status="executing")._value.get() == 3
        assert _metrics.jobs_by_status.labels(status="completed")._value.get() == 42
        assert _metrics.jobs_by_status.labels(status="failed")._value.get() == 1

    async def test_jobs_by_status_clears_old_labels(self):
        """A status that emptied between ticks must not keep its last value
        forever — refresh_gauges clears the label set first."""
        from app.observability import metrics as _metrics
        # Tick 1: 3 'executing', 0 'failed'.
        db1 = _mock_gauge_db(
            jobs_rows=[
                {"status": "executing", "c": 3},
                {"status": "failed", "c": 1},
            ],
            sessions_running=0, unresolved_window=0,
        )
        await _thresholds.refresh_gauges(db1)
        assert _metrics.jobs_by_status.labels(status="failed")._value.get() == 1

        # Tick 2: failed is gone (all jobs reaped) — gauge must reflect that.
        db2 = _mock_gauge_db(
            jobs_rows=[{"status": "executing", "c": 5}],
            sessions_running=0, unresolved_window=0,
        )
        await _thresholds.refresh_gauges(db2)
        # The 'failed' label should no longer have a sample at all OR
        # should be 0 — both forms satisfy the "not stuck at 1" invariant.
        # We assert via the new value (recreate the label series for read):
        try:
            val = _metrics.jobs_by_status.labels(status="failed")._value.get()
        except Exception:
            val = 0
        assert val == 0, f"failed gauge stuck at {val} after status emptied"

    async def test_research_sessions_running_gauge_set(self):
        from app.observability import metrics as _metrics
        db = _mock_gauge_db(
            jobs_rows=[], sessions_running=7, unresolved_window=0,
        )
        await _thresholds.refresh_gauges(db)
        assert _metrics.research_sessions_running._value.get() == 7

    async def test_unresolved_errors_window_gauge_set(self):
        from app.observability import metrics as _metrics
        db = _mock_gauge_db(
            jobs_rows=[], sessions_running=0, unresolved_window=15,
        )
        await _thresholds.refresh_gauges(db)
        assert _metrics.unresolved_errors_window._value.get() == 15

    async def test_jobs_query_failure_does_not_break_other_gauges(self):
        """fail-open: a jobs-query exception must not prevent the sessions
        + errors gauges from being refreshed."""
        from app.observability import metrics as _metrics
        db = _mock_gauge_db(
            jobs_rows=[], sessions_running=4, unresolved_window=2,
            fail_on_jobs=True,
        )
        # No exception propagates.
        await _thresholds.refresh_gauges(db)
        # Other two gauges still got their values.
        assert _metrics.research_sessions_running._value.get() == 4
        assert _metrics.unresolved_errors_window._value.get() == 2

    async def test_sessions_query_failure_does_not_break_other_gauges(self):
        from app.observability import metrics as _metrics
        db = _mock_gauge_db(
            jobs_rows=[{"status": "completed", "c": 8}],
            sessions_running=0, unresolved_window=3,
            fail_on_sessions=True,
        )
        await _thresholds.refresh_gauges(db)
        assert _metrics.jobs_by_status.labels(status="completed")._value.get() == 8
        assert _metrics.unresolved_errors_window._value.get() == 3

    async def test_errors_query_failure_does_not_break_other_gauges(self):
        from app.observability import metrics as _metrics
        db = _mock_gauge_db(
            jobs_rows=[{"status": "completed", "c": 2}],
            sessions_running=6, unresolved_window=0,
            fail_on_errors=True,
        )
        await _thresholds.refresh_gauges(db)
        assert _metrics.jobs_by_status.labels(status="completed")._value.get() == 2
        assert _metrics.research_sessions_running._value.get() == 6


# ---------------------------------------------------------------------------
# §17.192 — disabled-threshold property test (one row per disabled knob)
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestThresholdsDisabledBySettings:
    """A setting <= 0 disables the corresponding threshold check. Each
    threshold uses ``threshold > 0`` as the gate so a misconfigured 0 or
    negative value is treated as 'disable' rather than 'always fire'."""

    @pytest.mark.parametrize("knob,value", [
        ("alert_unresolved_errors_threshold", 0),
        ("alert_unresolved_errors_threshold", -1),
        ("alert_cost_window_usd_threshold", 0.0),
        ("alert_p95_latency_ms_threshold", 0),
    ])
    async def test_zero_or_negative_threshold_does_not_fire(
        self, monkeypatch, knob, value,
    ):
        monkeypatch.setattr(
            f"app.observability.thresholds.settings.{knob}", value, raising=False,
        )
        # Disable all OTHER thresholds so we isolate the one under test.
        for other in (
            "alert_unresolved_errors_threshold",
            "alert_cost_window_usd_threshold",
            "alert_p95_latency_ms_threshold",
        ):
            if other != knob:
                monkeypatch.setattr(
                    f"app.observability.thresholds.settings.{other}", 999_999,
                    raising=False,
                )
        db = _mock_db(unresolved=10_000)
        with patch(
            "app.observability.thresholds.observability_rollups.llm_rollup",
            new=AsyncMock(return_value={
                "total_cost_usd": 10_000.0,
                "by_model": [{"provider": "p", "model": "m", "latency_ms_p95": 999_999}],
            }),
        ):
            summary = await _thresholds.evaluate_thresholds(db)
        # The disabled knob's alert kind must not appear in fired.
        kind_for_knob = {
            "alert_unresolved_errors_threshold": "oncall.errors_unresolved",
            "alert_cost_window_usd_threshold": "cost.window_exceeded",
            "alert_p95_latency_ms_threshold": "latency.p95_exceeded",
        }[knob]
        fired_kinds = [k for k, _ in summary["fired"]]
        matches = [k for k in fired_kinds if k.startswith(kind_for_knob)]
        assert matches == [], (
            f"{knob}={value} should disable {kind_for_knob} alerts; "
            f"got fired: {matches}"
        )
