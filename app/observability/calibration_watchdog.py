"""Sprint X.26 — calibration cron watchdog.

The cron entry (`scripts/quarterly_calibration_pr.sh`) fires at 08:00 UTC
on the 1st of Jan/Apr/Jul/Oct. The script itself emits an alert on
non-zero exit (via the alerts CLI). What it cannot detect is the case
where cron itself didn't run at all — the user's crontab was disabled,
the laptop was asleep, anacron didn't make up the missed slot, etc.

This watchdog covers that gap: every tick, if today is one of the
quarterly fire dates and we're past the configured grace window, and no
``calibration.*`` alert and no successful run timestamp shows the cron
fired today, emit ``calibration.no_fire``. dedup_key is keyed to the
date, so the per-tick cadence is suppressed — but with the default
``alert_cooldown_seconds`` (1 h) the alert then re-fires roughly hourly
for the rest of a missed-cron day (an incident reminder, not a single
shot). Set ``alert_kind_cooldowns["calibration.no_fire"]`` (§17.388) to a
day-length cooldown if you want exactly one alert per missed quarter.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timezone, timedelta
from typing import Any

from sqlalchemy import text

from app.config import settings
from app.database import async_session
from app.observability import alerts as _alerts

logger = logging.getLogger("scaffold.calibration_watchdog")

# Fire months as configured in scripts/quarterly_calibration_pr.sh.
_QUARTER_MONTHS = (1, 4, 7, 10)
_FIRE_DAY = 1
_FIRE_HOUR_UTC = 8


def _is_fire_day(now_utc: datetime) -> bool:
    return now_utc.month in _QUARTER_MONTHS and now_utc.day == _FIRE_DAY


def _grace_elapsed(now_utc: datetime, grace_minutes: int) -> bool:
    fire_at = datetime.combine(
        now_utc.date(), time(_FIRE_HOUR_UTC, 0, tzinfo=timezone.utc),
    )
    return now_utc >= fire_at + timedelta(minutes=grace_minutes)


async def _saw_calibration_today(db, today: date) -> bool:
    """Return True if any calibration.* alert (start/ok/failed) was
    recorded today UTC, OR a successful run is recorded in the metric
    via the alert table itself. Treats either signal as "cron fired" —
    the script's first action is to emit calibration.started, so the
    presence of any same-day calibration alert is sufficient evidence.
    """
    try:
        row = await db.execute(
            text(
                "SELECT 1 FROM system_alerts "
                "WHERE kind LIKE 'calibration.%' "
                "  AND created_at::date = :d "
                "LIMIT 1"
            ),
            {"d": today},
        )
        return row.first() is not None
    except Exception as exc:
        logger.debug("watchdog_db_probe_failed: err=%s", exc)
        # Fail SAFE: if we can't tell, assume the cron fired. This avoids
        # false-positive 'no_fire' alerts when the DB is briefly degraded.
        return True


async def check(now_utc: datetime | None = None) -> dict[str, Any]:
    """Single watchdog evaluation. Returns a result dict for tests +
    structured logging. Never raises."""
    if not settings.calibration_watchdog_enabled:
        return {"checked": False, "reason": "disabled"}

    now_utc = now_utc or datetime.now(timezone.utc)
    if not _is_fire_day(now_utc):
        return {"checked": False, "reason": "not_a_fire_day", "now": now_utc.isoformat()}
    if not _grace_elapsed(now_utc, settings.calibration_grace_minutes):
        return {"checked": False, "reason": "within_grace", "now": now_utc.isoformat()}

    try:
        async with async_session() as db:
            saw = await _saw_calibration_today(db, now_utc.date())
            if saw:
                return {"checked": True, "fired_alert": False, "reason": "saw_calibration_today"}
            # §17.194 — stable event-name log line at CRITICAL level so an
            # operator grepping journald sees the drift without needing to
            # query the alerts table. The alert itself (below) is the
            # operator-facing surface; this log line is the
            # forwarder/audit-trail surface. Pattern: `event="<kind>"` so
            # the grep is the same as any other structured event line.
            logger.critical(
                'event="calibration.no_fire" expected_fire_date=%s '
                'grace_minutes=%d msg=%s',
                now_utc.date().isoformat(),
                settings.calibration_grace_minutes,
                "Quarterly calibration cron did not fire and grace elapsed",
            )
            await _alerts.emit(
                kind="calibration.no_fire",
                severity="critical",
                message=(
                    f"Quarterly calibration cron did not fire on {now_utc.date()} "
                    f"(grace={settings.calibration_grace_minutes}m elapsed). "
                    f"Check user crontab for scripts/quarterly_calibration_pr.sh."
                ),
                payload={
                    "expected_fire_date": now_utc.date().isoformat(),
                    "grace_minutes": settings.calibration_grace_minutes,
                },
                dedup_key=f"calibration.no_fire:{now_utc.date().isoformat()}",
                db=db,
            )
            return {"checked": True, "fired_alert": True}
    except Exception as exc:
        logger.error('event="watchdog_failed" err=%s', exc)
        return {"checked": True, "fired_alert": False, "reason": f"error:{exc}"}


async def tick() -> None:
    """Scheduler entrypoint. Single attempt; result + reason flow into
    the standard log stream so a long quiet period is still auditable."""
    result = await check()
    logger.debug('event="calibration_watchdog_tick" result=%r', result)
