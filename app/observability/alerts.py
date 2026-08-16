"""Sprint X.26 — alert sinks.

Two sinks, both always-on when configured:

  * **DB sink** — INSERT into ``system_alerts``. Always attempted unless
    ``alert_db_enabled`` is False. Source of truth for ``GET /observability/alerts``.
  * **File sink** — append one JSON line per alert to ``alert_file_path``
    when set. Cheap, useful when there's no external receiver.

A third side-effect, free of charge: every emit() also calls
``logger.error()``/``warning()`` so anything tailing the existing
structlog stream sees the alert without subscribing to either sink.

Dedup: when ``dedup_key`` is provided, repeated emits of the same key
within ``alert_cooldown_seconds`` are suppressed. The lookup is a single
indexed query (idx_system_alerts_dedup_key_created); the cooldown is
configurable per deployment.

Also exposes a CLI entrypoint (``python -m app.observability.alerts``)
so the calibration cron script can emit alerts without an HTTP hop —
the orchestrator may be down when cron fires.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.config import settings

logger = logging.getLogger("scaffold.alerts")

_VALID_SEVERITIES = ("info", "warning", "critical")


# ── Dedup ────────────────────────────────────────────────────────────

def _resolve_cooldown(kind: str, override: int | None = None) -> int:
    """§17.388 — pick the dedup-cooldown for a given alert kind.

    Precedence (most specific wins):

      1. ``override`` kwarg (emit-time, e.g. caller pinned the cooldown
         for one specific event).
      2. ``settings.alert_kind_cooldowns[kind]`` (deployment-tuned per-kind).
      3. ``settings.alert_cooldown_seconds`` (default for every kind).

    Pre-§17.388 the resolution was always step 3 — a single uniform
    cooldown for every kind. §17.388 keeps that as the floor but lets
    operators tune busy kinds (e.g. shorter cooldown for
    ``host.oom_killed`` so a multi-victim episode doesn't suppress
    distinct comm names) without touching code.
    """
    if override is not None:
        return max(0, int(override))
    per_kind = (settings.alert_kind_cooldowns or {}).get(kind)
    if per_kind is not None:
        return max(0, int(per_kind))
    return settings.alert_cooldown_seconds


async def _is_in_cooldown(db, dedup_key: str, cooldown_seconds: int) -> bool:
    """Return True if `dedup_key` was emitted within the cooldown window."""
    if not dedup_key or cooldown_seconds <= 0:
        return False
    try:
        row = await db.execute(
            text(
                "SELECT 1 FROM system_alerts "
                "WHERE dedup_key = :k "
                "  AND created_at >= NOW() - make_interval(secs => :w) "
                "LIMIT 1"
            ),
            {"k": dedup_key, "w": cooldown_seconds},
        )
        return row.first() is not None
    except Exception as exc:
        logger.debug("alert_dedup_check_failed: err=%s (assuming not in cooldown)", exc)
        return False


# ── File sink ───────────────────────────────────────────────────────

def _write_file_sink(path: str, record: dict[str, Any]) -> None:
    """Append a single JSONL line. Best-effort: any IO error is logged
    and swallowed — alerting must never raise."""
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception as exc:
        logger.warning("alert_file_sink_failed: path=%s err=%s", path, exc)


# ── Public emit ─────────────────────────────────────────────────────

async def emit(
    *,
    kind: str,
    severity: str = "warning",
    message: str,
    payload: dict[str, Any] | None = None,
    dedup_key: str | None = None,
    db=None,
    cooldown_seconds: int | None = None,
) -> dict[str, Any]:
    """Emit one alert through all configured sinks.

    Returns a dict describing what happened: `{"emitted": bool,
    "suppressed": bool, "id": str | None, "reason": str | None}`. Never
    raises — sink failures are logged and absorbed.

    `db` is optional; when omitted, a short-lived session is opened and
    closed here. **If you DO pass a session, emit() commits it** — the alert
    INSERT must persist independently of any caller rollback, so emit issues
    its own ``db.commit()``. Only pass a session whose pending work you are
    happy to have committed. In practice the only callers that pass `db` are
    the read-only periodic ticks (thresholds / calibration_watchdog), whose
    sessions hold nothing but the alert itself and which rely on this commit
    to persist it. Most callers omit `db`.

    `cooldown_seconds` is optional and overrides both the per-kind
    setting and the global default for this call only. Use when a
    specific event has different cadence than the kind's typical
    pattern (e.g., a burst-mode test or a one-off probe). Most
    callers omit it and rely on the per-kind setting resolution
    via `_resolve_cooldown` — see §17.388.
    """
    from app.observability import metrics as _metrics  # local import: keep test isolation simple

    severity = severity.lower().strip()
    if severity not in _VALID_SEVERITIES:
        severity = "warning"

    record = {
        "kind": kind,
        "severity": severity,
        "message": message,
        "payload": payload or {},
        "dedup_key": dedup_key,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    # Dedup gate. A standalone DB session is opened only if the caller
    # didn't pass one — keeps the dedup probe outside the caller's tx.
    cooldown = _resolve_cooldown(kind, cooldown_seconds)
    own_db = db is None
    if own_db:
        try:
            from app.database import async_session
            db_cm = async_session()
            db = await db_cm.__aenter__()
        except Exception as exc:
            # No DB → still write file sink + log so the alert is not lost.
            logger.error(
                "alert_db_session_open_failed: kind=%s severity=%s msg=%s err=%s",
                kind, severity, message, exc,
            )
            db = None
            db_cm = None
    else:
        db_cm = None

    try:
        if db is not None and dedup_key:
            if await _is_in_cooldown(db, dedup_key, cooldown):
                try:
                    _metrics.alerts_suppressed_total.labels(kind=kind).inc()
                except Exception:
                    pass
                logger.debug(
                    "alert_suppressed: kind=%s dedup_key=%s cooldown_s=%d",
                    kind, dedup_key, cooldown,
                )
                return {"emitted": False, "suppressed": True, "id": None, "reason": "cooldown"}

        # Always-on logger leg — the user gets the alert in journald/docker
        # logs even if both sinks fail.
        log_fn = (
            logger.critical if severity == "critical"
            else logger.warning if severity == "warning"
            else logger.info
        )
        log_fn(
            'event="alert_emitted" kind=%s severity=%s msg=%r dedup_key=%s',
            kind, severity, message, dedup_key,
        )

        # DB sink
        alert_id: str | None = None
        if db is not None and settings.alert_db_enabled:
            try:
                row = await db.execute(
                    text(
                        "INSERT INTO system_alerts "
                        "(kind, severity, message, payload, dedup_key) "
                        "VALUES (:kind, :sev, :msg, CAST(:payload AS JSONB), :dk) "
                        "RETURNING id"
                    ),
                    {
                        "kind": kind,
                        "sev": severity,
                        "msg": message,
                        "payload": json.dumps(record["payload"]),
                        "dk": dedup_key,
                    },
                )
                alert_id = str(row.scalar())
                await db.commit()
            except Exception as exc:
                # §17.598 — roll back so a failed INSERT doesn't leave the
                # (possibly caller-shared) session in a PendingRollbackError
                # state that fail-opens every later read/emit in the same
                # evaluate_thresholds tick. Defensive: rollback may itself fail
                # on a dead connection.
                try:
                    await db.rollback()
                except Exception:
                    pass
                logger.warning(
                    "alert_db_insert_failed: kind=%s err=%s (other sinks still fired)",
                    kind, exc,
                )

        # File sink — §17.412: the append is blocking open()+write(); run it
        # off the event loop so a slow/stalled disk (e.g. NFS) can't lag the
        # scheduler tick that emit() runs under.
        if settings.alert_file_path:
            record_with_id = dict(record)
            if alert_id:
                record_with_id["id"] = alert_id
            await asyncio.to_thread(
                _write_file_sink, settings.alert_file_path, record_with_id,
            )

        try:
            _metrics.alerts_emitted_total.labels(kind=kind, severity=severity).inc()
        except Exception:
            pass

        return {"emitted": True, "suppressed": False, "id": alert_id, "reason": None}
    finally:
        if own_db and db_cm is not None:
            try:
                await db_cm.__aexit__(None, None, None)
            except Exception:
                logger.debug("alert_db_session_close_failed", exc_info=True)


# ── Read helpers (used by app/routers/alerts.py) ────────────────────

async def list_recent(
    *, kind: str | None = None, since_minutes: int | None = None,
    limit: int = 100, db,
) -> dict[str, Any]:
    """Recent system_alerts rows. Fail-open: empty list on any DB error."""
    try:
        # asyncpg's prepared-statement protocol can't type-infer a bare
        # `:param IS NULL` — explicit casts disambiguate without changing
        # behavior. Same pattern as `(CAST(:payload AS JSONB))` in emit().
        rows = await db.execute(
            text(
                "SELECT id, kind, severity, message, payload, dedup_key, created_at "
                "FROM system_alerts "
                "WHERE (CAST(:kind AS TEXT) IS NULL OR kind = CAST(:kind AS TEXT)) "
                "  AND (CAST(:since AS INTEGER) IS NULL "
                "       OR created_at >= NOW() - make_interval(mins => CAST(:since AS INTEGER))) "
                "ORDER BY created_at DESC LIMIT :limit"
            ),
            {"kind": kind, "since": since_minutes, "limit": limit},
        )
        records = rows.mappings().all()
    except Exception as exc:
        logger.debug("alerts_list_recent_failed: err=%s (returning empty)", exc)
        records = []
    alerts = [
        {
            "id": str(r["id"]),
            "kind": r["kind"],
            "severity": r["severity"],
            "message": r["message"],
            "payload": r["payload"] or {},
            "dedup_key": r["dedup_key"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in records
    ]
    return {
        "filters": {"kind": kind, "since_minutes": since_minutes, "limit": limit},
        "count": len(alerts),
        "alerts": alerts,
    }


# ── CLI entrypoint ──────────────────────────────────────────────────
#
# Invoked from `scripts/quarterly_calibration_pr.sh` (and operators) so
# the bash side can emit alerts without an HTTP hop. Exit code 0 always
# unless `--strict` is passed; the script must keep going even if
# alerting is broken.

def _parse_argv(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="app.observability.alerts")
    sub = p.add_subparsers(dest="cmd", required=True)
    em = sub.add_parser("emit", help="Emit an alert")
    em.add_argument("--kind", required=True)
    em.add_argument("--severity", default="warning", choices=_VALID_SEVERITIES)
    em.add_argument("--message", required=True)
    em.add_argument("--payload", default="", help="JSON string (optional)")
    em.add_argument("--dedup-key", default="", dest="dedup_key")
    em.add_argument(
        "--cooldown-seconds", default=None, type=int, dest="cooldown_seconds",
        help=(
            "§17.388 — override the dedup cooldown for THIS emit only "
            "(beats both alert_kind_cooldowns and alert_cooldown_seconds). "
            "Useful for one-off scripts that need a tighter or looser "
            "window than the kind's default."
        ),
    )
    em.add_argument("--strict", action="store_true",
                    help="Exit non-zero on emit failure (default: always 0).")
    return p.parse_args(argv)


async def _cli_emit(ns: argparse.Namespace) -> int:
    payload: dict[str, Any] = {}
    if ns.payload:
        try:
            payload = json.loads(ns.payload)
            if not isinstance(payload, dict):
                payload = {"value": payload}
        except json.JSONDecodeError:
            payload = {"raw": ns.payload}
    try:
        result = await emit(
            kind=ns.kind, severity=ns.severity, message=ns.message,
            payload=payload, dedup_key=ns.dedup_key or None,
            cooldown_seconds=ns.cooldown_seconds,
        )
    except Exception as exc:
        sys.stderr.write(f"alert_cli_emit_failed: {exc}\n")
        return 1 if ns.strict else 0
    sys.stdout.write(json.dumps(result) + "\n")
    if ns.strict and not result.get("emitted"):
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    ns = _parse_argv(argv if argv is not None else sys.argv[1:])
    if ns.cmd == "emit":
        return asyncio.run(_cli_emit(ns))
    return 1


if __name__ == "__main__":
    # The CLI runs against the orchestrator's same DB DSN by default;
    # the cron script doesn't need to override it.
    sys.exit(main())
