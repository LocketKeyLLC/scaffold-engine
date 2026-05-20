"""Sprint X.26 — calibration cron no-fire watchdog."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.observability import calibration_watchdog as _watchdog


@pytest.mark.smoke
class TestWatchdogGate:
    async def test_skips_when_disabled(self, monkeypatch):
        monkeypatch.setattr(
            "app.observability.calibration_watchdog.settings.calibration_watchdog_enabled",
            False, raising=False,
        )
        result = await _watchdog.check(now_utc=datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc))
        assert result == {"checked": False, "reason": "disabled"}

    async def test_skips_when_not_a_fire_day(self, monkeypatch):
        monkeypatch.setattr(
            "app.observability.calibration_watchdog.settings.calibration_watchdog_enabled",
            True, raising=False,
        )
        # 2026-05-09 — not a quarter-start day
        result = await _watchdog.check(now_utc=datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc))
        assert result["checked"] is False
        assert result["reason"] == "not_a_fire_day"

    async def test_skips_within_grace_window(self, monkeypatch):
        monkeypatch.setattr(
            "app.observability.calibration_watchdog.settings.calibration_watchdog_enabled",
            True, raising=False,
        )
        monkeypatch.setattr(
            "app.observability.calibration_watchdog.settings.calibration_grace_minutes",
            120, raising=False,
        )
        # 2026-07-01 09:00 UTC → fire was 08:00, grace is 120 min, so still grace.
        result = await _watchdog.check(now_utc=datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc))
        assert result["checked"] is False
        assert result["reason"] == "within_grace"


def _watchdog_db(saw_today: bool):
    """Build an AsyncMock db; returns (1,) from the saw-today probe when
    saw_today=True, else None. Also handles dedup probe + INSERT."""

    async def _execute(sql, params=None):
        sql_text = str(sql)
        result = MagicMock()
        if "WHERE kind LIKE 'calibration.%'" in sql_text:
            result.first.return_value = (1,) if saw_today else None
            return result
        if "WHERE dedup_key" in sql_text:
            result.first.return_value = None
            return result
        if "INSERT INTO system_alerts" in sql_text:
            result.scalar.return_value = "wd-1"
            return result
        return result

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_execute)
    db.commit = AsyncMock()
    return db


@pytest.mark.smoke
class TestWatchdogFire:
    async def test_fires_when_grace_elapsed_and_no_calibration_today(self, monkeypatch):
        monkeypatch.setattr(
            "app.observability.calibration_watchdog.settings.calibration_watchdog_enabled",
            True, raising=False,
        )
        monkeypatch.setattr(
            "app.observability.calibration_watchdog.settings.calibration_grace_minutes",
            60, raising=False,
        )

        db = _watchdog_db(saw_today=False)

        class _Ctx:
            async def __aenter__(self_): return db
            async def __aexit__(self_, *a): return False

        with patch(
            "app.observability.calibration_watchdog.async_session",
            return_value=_Ctx(),
        ):
            now = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc)  # 1.5h past 08:00
            result = await _watchdog.check(now_utc=now)

        assert result["checked"] is True
        assert result["fired_alert"] is True

    async def test_no_fire_when_calibration_already_recorded_today(self, monkeypatch):
        monkeypatch.setattr(
            "app.observability.calibration_watchdog.settings.calibration_watchdog_enabled",
            True, raising=False,
        )
        monkeypatch.setattr(
            "app.observability.calibration_watchdog.settings.calibration_grace_minutes",
            60, raising=False,
        )

        db = _watchdog_db(saw_today=True)

        class _Ctx:
            async def __aenter__(self_): return db
            async def __aexit__(self_, *a): return False

        with patch(
            "app.observability.calibration_watchdog.async_session",
            return_value=_Ctx(),
        ):
            now = datetime(2026, 7, 1, 10, 30, tzinfo=timezone.utc)
            result = await _watchdog.check(now_utc=now)

        assert result["checked"] is True
        assert result["fired_alert"] is False
        assert result["reason"] == "saw_calibration_today"


# ---------------------------------------------------------------------------
# §17.194 — stable critical log line on drift emission
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestWatchdogCriticalLogLine:
    """The audit (AUDIT.md 3.6) asked for "a critical-level log line on
    drift uses a stable event name for grepping." Pre-§17.194 the
    no_fire path only emitted the alert to the DB — operators tailing
    journald saw nothing at CRITICAL level. The new logger.critical
    line uses ``event="calibration.no_fire"`` as the stable grep token."""

    async def test_critical_log_emitted_on_no_fire(self, monkeypatch, caplog):
        import logging
        monkeypatch.setattr(
            "app.observability.calibration_watchdog.settings.calibration_watchdog_enabled",
            True, raising=False,
        )
        monkeypatch.setattr(
            "app.observability.calibration_watchdog.settings.calibration_grace_minutes",
            60, raising=False,
        )

        db = _watchdog_db(saw_today=False)

        class _Ctx:
            async def __aenter__(self_): return db
            async def __aexit__(self_, *a): return False

        with patch(
            "app.observability.calibration_watchdog.async_session",
            return_value=_Ctx(),
        ):
            with caplog.at_level(logging.CRITICAL, logger="scaffold.calibration_watchdog"):
                now = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc)
                await _watchdog.check(now_utc=now)

        # The stable event-name grep token must appear at CRITICAL.
        critical_records = [
            r for r in caplog.records
            if r.levelno == logging.CRITICAL
        ]
        assert critical_records, "expected at least one CRITICAL log record"
        joined = " ".join(r.message for r in critical_records)
        assert 'event="calibration.no_fire"' in joined, (
            f"expected stable event name in critical log; got: {joined}"
        )
        # The date and grace minutes should be on the same line for
        # operator triage without needing to read the DB.
        assert "2026-07-01" in joined
        assert "grace_minutes=60" in joined

    async def test_no_critical_log_when_calibration_already_seen(self, monkeypatch, caplog):
        """Sanity: the critical line fires ONLY on the no_fire branch — a
        normal "saw calibration today" path emits no CRITICAL."""
        import logging
        monkeypatch.setattr(
            "app.observability.calibration_watchdog.settings.calibration_watchdog_enabled",
            True, raising=False,
        )
        monkeypatch.setattr(
            "app.observability.calibration_watchdog.settings.calibration_grace_minutes",
            60, raising=False,
        )

        db = _watchdog_db(saw_today=True)

        class _Ctx:
            async def __aenter__(self_): return db
            async def __aexit__(self_, *a): return False

        with patch(
            "app.observability.calibration_watchdog.async_session",
            return_value=_Ctx(),
        ):
            with caplog.at_level(logging.CRITICAL, logger="scaffold.calibration_watchdog"):
                now = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
                await _watchdog.check(now_utc=now)

        critical_records = [
            r for r in caplog.records
            if r.levelno == logging.CRITICAL
        ]
        assert critical_records == [], (
            f"unexpected CRITICAL records on happy path: {[r.message for r in critical_records]}"
        )
