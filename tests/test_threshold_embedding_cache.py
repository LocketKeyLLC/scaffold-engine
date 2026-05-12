"""Tests for the §17.132 embedding-cache pressure alert.

Exercises the `_check_embedding_cache_pressure` branch of
`evaluate_thresholds`. Verifies:
  - first tick establishes baseline silently
  - both-conditions-met fires the alert
  - either-condition-only does NOT fire
  - threshold=0 disables the check
  - dedup_key carried so sustained pressure doesn't spam
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.observability import thresholds as _thresholds


def _mock_db():
    """Permissive db mock: dedup probes always miss; everything else returns
    an empty MagicMock. Suits the threshold flow which does (a) rollup probes
    handled via patched llm_rollup, (b) alert dedup probe via the FROM
    system_alerts WHERE dedup_key path, (c) the alert INSERT."""

    async def _execute(sql, params=None):
        sql_text = str(sql)
        result = MagicMock()
        if "FROM error_logs" in sql_text and "resolved = FALSE" in sql_text:
            result.scalar.return_value = 0
            return result
        if "FROM system_alerts" in sql_text and "WHERE dedup_key" in sql_text:
            result.first.return_value = None
            return result
        if "INSERT INTO system_alerts" in sql_text:
            result.scalar.return_value = "alert-id"
            return result
        # Anything else (jobs-by-status, etc.) — empty mapping iterator
        result.mappings.return_value.all.return_value = []
        return result

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_execute)
    db.commit = AsyncMock()
    return db


def _quiet_other_alerts(monkeypatch):
    """Set the other three thresholds high so they don't fire and
    pollute summary["fired"] in cache-only tests."""
    monkeypatch.setattr(
        "app.observability.thresholds.settings.alert_unresolved_errors_threshold",
        10_000, raising=False,
    )
    monkeypatch.setattr(
        "app.observability.thresholds.settings.alert_cost_window_usd_threshold",
        10_000.0, raising=False,
    )
    monkeypatch.setattr(
        "app.observability.thresholds.settings.alert_p95_latency_ms_threshold",
        999_999_999, raising=False,
    )
    monkeypatch.setattr(
        "app.observability.thresholds.settings.alert_eval_window_minutes", 60,
        raising=False,
    )


@pytest.fixture(autouse=True)
def clean_snapshot():
    """Each test gets a fresh baseline so order can't influence outcome."""
    _thresholds._reset_embedding_snapshot()
    yield
    _thresholds._reset_embedding_snapshot()


@pytest.fixture
def alert_thresholds_on(monkeypatch):
    monkeypatch.setattr(
        "app.observability.thresholds.settings.alert_embedding_evictions_threshold",
        100, raising=False,
    )
    monkeypatch.setattr(
        "app.observability.thresholds.settings.alert_embedding_hit_rate_floor",
        0.5, raising=False,
    )


def _patch_cache_stats(stats: dict):
    """Monkey-patch the cache singleton so its stats match `stats`."""
    fake_cache = MagicMock()
    fake_cache.stats = stats
    return patch(
        "app.utils.embedding_cache.get_cache",
        new=MagicMock(return_value=fake_cache),
    )


def _empty_rollup_patch():
    return patch(
        "app.observability.thresholds.observability_rollups.llm_rollup",
        new=AsyncMock(return_value={"total_cost_usd": 0.0, "by_model": []}),
    )


async def test_first_tick_establishes_baseline_no_alert(monkeypatch, alert_thresholds_on):
    _quiet_other_alerts(monkeypatch)
    db = _mock_db()
    stats = {"hits": 50, "misses": 20, "evictions": 0, "memory_size": 70}
    with _patch_cache_stats(stats), _empty_rollup_patch():
        summary = await _thresholds.evaluate_thresholds(db)

    assert summary["fired"] == []
    assert summary["embedding_cache"]["baseline_established"] is True
    # Snapshot now carries the seed values
    assert _thresholds._prev_embedding_snapshot == {
        "hits": 50, "misses": 20, "evictions": 0,
    }


async def test_pressure_fires_when_both_conditions_met(monkeypatch, alert_thresholds_on):
    """Tick 1 baseline; tick 2 sees 200 evictions + 30% hit rate → fire."""
    _quiet_other_alerts(monkeypatch)
    db = _mock_db()

    # First tick — baseline
    with _patch_cache_stats(
        {"hits": 100, "misses": 50, "evictions": 0, "memory_size": 150}
    ), _empty_rollup_patch():
        await _thresholds.evaluate_thresholds(db)

    # Second tick — d_hits=30, d_misses=70 → interval_hit_rate=0.3 < 0.5,
    # d_evictions=200 ≥ 100 → BOTH conditions met
    with _patch_cache_stats(
        {"hits": 130, "misses": 120, "evictions": 200, "memory_size": 9000}
    ), _empty_rollup_patch():
        summary = await _thresholds.evaluate_thresholds(db)

    fired_kinds = [k for k, _ in summary["fired"]]
    assert "cache.embedding_pressure" in fired_kinds
    audit = summary["embedding_cache"]
    assert audit["fired"] is True
    assert audit["delta_evictions"] == 200
    assert audit["delta_hits"] == 30
    assert audit["delta_misses"] == 70
    assert audit["interval_hit_rate"] == 0.3


async def test_no_fire_when_evictions_below_threshold(monkeypatch, alert_thresholds_on):
    """Hit rate is low but evictions count is below threshold → no alert.
    Avoids alerting during cold start where the cache hasn't filled yet."""
    _quiet_other_alerts(monkeypatch)
    db = _mock_db()

    with _patch_cache_stats(
        {"hits": 0, "misses": 0, "evictions": 0, "memory_size": 0}
    ), _empty_rollup_patch():
        await _thresholds.evaluate_thresholds(db)

    # 10 evictions (well under 100), hit_rate 0.2 (under floor)
    with _patch_cache_stats(
        {"hits": 10, "misses": 40, "evictions": 10, "memory_size": 50}
    ), _empty_rollup_patch():
        summary = await _thresholds.evaluate_thresholds(db)

    fired_kinds = [k for k, _ in summary["fired"]]
    assert "cache.embedding_pressure" not in fired_kinds
    assert summary["embedding_cache"]["fired"] is False


async def test_no_fire_when_hit_rate_above_floor(monkeypatch, alert_thresholds_on):
    """Many evictions but the cache is still earning its keep (high hit
    rate) — that's a hot working set wider than memory, not a sizing bug
    we need to alert on."""
    _quiet_other_alerts(monkeypatch)
    db = _mock_db()

    with _patch_cache_stats(
        {"hits": 0, "misses": 0, "evictions": 0, "memory_size": 0}
    ), _empty_rollup_patch():
        await _thresholds.evaluate_thresholds(db)

    # 200 evictions (≥ threshold) but hit_rate 0.8 (above floor)
    with _patch_cache_stats(
        {"hits": 800, "misses": 200, "evictions": 200, "memory_size": 9000}
    ), _empty_rollup_patch():
        summary = await _thresholds.evaluate_thresholds(db)

    fired_kinds = [k for k, _ in summary["fired"]]
    assert "cache.embedding_pressure" not in fired_kinds
    audit = summary["embedding_cache"]
    assert audit["fired"] is False
    assert audit["interval_hit_rate"] == 0.8


async def test_threshold_zero_disables(monkeypatch):
    """alert_embedding_evictions_threshold=0 → check returns disabled,
    no baseline established, no emit even on a pressure-shaped tick."""
    _quiet_other_alerts(monkeypatch)
    monkeypatch.setattr(
        "app.observability.thresholds.settings.alert_embedding_evictions_threshold",
        0, raising=False,
    )
    db = _mock_db()

    with _patch_cache_stats(
        {"hits": 10, "misses": 1000, "evictions": 999, "memory_size": 9000}
    ), _empty_rollup_patch():
        summary = await _thresholds.evaluate_thresholds(db)

    fired_kinds = [k for k, _ in summary["fired"]]
    assert "cache.embedding_pressure" not in fired_kinds
    assert summary["embedding_cache"] == {"disabled": True}
    # Snapshot stays empty so re-enabling later doesn't carry stale baseline
    assert _thresholds._prev_embedding_snapshot == {}


async def test_alert_payload_carries_dedup_key(monkeypatch, alert_thresholds_on):
    """Verify the emitted alert uses dedup_key='cache.embedding_pressure'
    so sustained pressure de-duplicates within alert_cooldown_seconds."""
    _quiet_other_alerts(monkeypatch)
    db = _mock_db()
    emitted_calls = []

    async def _spy_emit(**kwargs):
        emitted_calls.append(kwargs)
        return {"emitted": True, "suppressed": False, "id": "x", "reason": None}

    with _patch_cache_stats(
        {"hits": 0, "misses": 0, "evictions": 0, "memory_size": 0}
    ), _empty_rollup_patch():
        await _thresholds.evaluate_thresholds(db)

    with _patch_cache_stats(
        {"hits": 10, "misses": 90, "evictions": 500, "memory_size": 9000}
    ), _empty_rollup_patch(), patch(
        "app.observability.thresholds._alerts.emit", new=_spy_emit,
    ):
        await _thresholds.evaluate_thresholds(db)

    pressure_calls = [c for c in emitted_calls if c.get("kind") == "cache.embedding_pressure"]
    assert len(pressure_calls) == 1
    call = pressure_calls[0]
    assert call["dedup_key"] == "cache.embedding_pressure"
    assert call["severity"] == "warning"
    payload = call["payload"]
    assert payload["delta_evictions"] == 500
    assert payload["delta_hits"] == 10
    assert payload["delta_misses"] == 90
    assert payload["interval_hit_rate"] == 0.1
    assert payload["memory_size_setting"] >= 0  # whatever settings says


async def test_snapshot_updates_even_when_not_firing(monkeypatch, alert_thresholds_on):
    """The interval baseline must advance every tick, even quiet ones,
    so a slow-growing leak still gets caught when it crosses threshold."""
    _quiet_other_alerts(monkeypatch)
    db = _mock_db()

    with _patch_cache_stats(
        {"hits": 100, "misses": 100, "evictions": 0, "memory_size": 200}
    ), _empty_rollup_patch():
        await _thresholds.evaluate_thresholds(db)

    # Quiet tick — no pressure
    with _patch_cache_stats(
        {"hits": 200, "misses": 110, "evictions": 5, "memory_size": 305}
    ), _empty_rollup_patch():
        await _thresholds.evaluate_thresholds(db)

    assert _thresholds._prev_embedding_snapshot == {
        "hits": 200, "misses": 110, "evictions": 5,
    }
