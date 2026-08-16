#!/usr/bin/env python3
"""§17.161 — host-side OOM event watcher for scaffold-engine.

Reads ``docker events --filter event=oom`` and emits a
``container.oom_killed`` alert via the orchestrator's existing
``app.observability.alerts`` CLI for every OOMKilled event on a
compose-labelled scaffold-engine container.

Runs as the operator user (must be in the ``docker`` group). No Docker
socket exposure inside any container — keeps the §17.64 hardening
posture intact.

Operator install:

    sudo cp scripts/scaffold-oom-watcher.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now scaffold-oom-watcher

Test event injection:

    python3 scripts/oom_watcher.py --test-event '{"Type":"container",
      "Action":"oom","Actor":{"ID":"abc","Attributes":{"name":"scaffold-orchestrator",
      "com.docker.compose.project":"scaffold-engine"}},"time":1700000000}'

The ``--dry-run`` flag prints the emit argv instead of executing it,
so the watcher can be inspected without writing to ``system_alerts``.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import subprocess
import sys
import time
from typing import Iterable, Optional

DEFAULT_PROJECT = "scaffold-engine"
DEFAULT_ORCHESTRATOR = "scaffold-orchestrator"
ALERT_KIND = "container.oom_killed"
ALERT_SEVERITY = "critical"
EMIT_MAX_RETRIES = 4
EMIT_BACKOFF_BASE_S = 1.0
EMIT_BACKOFF_CAP_S = 30.0

log = logging.getLogger("oom_watcher")


def parse_event(line: str) -> Optional[dict]:
    """Parse one line of ``docker events --format '{{json .}}'`` output.

    Returns the parsed dict, or None for blank / malformed lines.
    """
    line = line.strip()
    if not line:
        return None
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return None
    return ev if isinstance(ev, dict) else None


def is_compose_managed_oom(event: dict, project: str) -> bool:
    """Filter to OOM events on containers in the given compose project.

    docker events for OOM kills set ``Type=container`` and ``Action=oom``.
    Compose tags every container it manages with the
    ``com.docker.compose.project`` label so we can ignore one-off
    ``docker run`` containers that happen to OOM on the same host.
    """
    if event.get("Type") != "container":
        return False
    if event.get("Action") != "oom":
        return False
    attrs = (event.get("Actor") or {}).get("Attributes") or {}
    return attrs.get("com.docker.compose.project") == project


def _event_time_iso(event: dict) -> str:
    """Convert docker event's epoch ``time`` into UTC ISO-8601."""
    t = event.get("time")
    if not isinstance(t, (int, float)):
        t = time.time()
    return _dt.datetime.fromtimestamp(t, tz=_dt.timezone.utc).isoformat()


def build_emit_argv(event: dict, orchestrator: str) -> list[str]:
    """Compose the ``docker exec ... python -m app.observability.alerts emit`` argv.

    Dedup key is the container name so repeated OOMs of the same
    service are rate-limited by ``alert_cooldown_seconds`` (default
    1 h) — one row per container per cooldown window.
    """
    attrs = (event.get("Actor") or {}).get("Attributes") or {}
    name = attrs.get("name") or "<unknown>"
    image = attrs.get("image") or ""
    cid_full = (event.get("Actor") or {}).get("ID") or ""
    cid_short = cid_full[:12]

    payload = {
        "container_name": name,
        "container_id": cid_short,
        "image": image,
        "event_time_utc": _event_time_iso(event),
    }
    message = (
        f"Container {name} was OOMKilled — kernel hit container mem_limit."
        " Inspect docker stats and consider raising the cap for this service."
    )
    return [
        "docker", "exec", orchestrator,
        "python", "-m", "app.observability.alerts", "emit",
        "--kind", ALERT_KIND,
        "--severity", ALERT_SEVERITY,
        "--message", message,
        "--payload", json.dumps(payload, separators=(",", ":")),
        "--dedup-key", f"{ALERT_KIND}:{name}",
    ]


def _run_emit_with_retry(argv: list[str]) -> bool:
    """Run the emit argv with bounded exponential backoff.

    Returns True if the emit succeeded (exit 0). Returns False if
    every retry failed — the orchestrator is presumably down (e.g.
    it was the OOM victim and hasn't restarted yet). Logs to stderr
    via the module logger; never raises.
    """
    delay = EMIT_BACKOFF_BASE_S
    for attempt in range(1, EMIT_MAX_RETRIES + 1):
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=15)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            log.warning("emit_failed attempt=%d err=%r", attempt, exc)
        else:
            if r.returncode == 0:
                log.info("emit_ok attempt=%d stdout=%s", attempt, r.stdout.strip())
                return True
            log.warning(
                "emit_nonzero attempt=%d rc=%d stderr=%s",
                attempt, r.returncode, r.stderr.strip(),
            )
        if attempt < EMIT_MAX_RETRIES:
            time.sleep(delay)
            delay = min(delay * 2, EMIT_BACKOFF_CAP_S)
    log.error("emit_giving_up after=%d attempts", EMIT_MAX_RETRIES)
    return False


def _event_source(args: argparse.Namespace) -> Iterable[str]:
    """Yield event-line strings from the configured source.

    Three modes:
      * ``--test-event '<json>'`` — yield that one line and stop. Used by tests.
      * ``--stdin`` — read JSON lines from stdin (one event per line).
      * default — exec ``docker events --filter event=oom`` and stream stdout.
    """
    if args.test_event is not None:
        yield args.test_event
        return
    if args.stdin:
        for line in sys.stdin:
            yield line
        return
    cmd = [
        "docker", "events",
        "--filter", "type=container",
        "--filter", "event=oom",
        "--format", "{{json .}}",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, bufsize=1)
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            yield line
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def run(args: argparse.Namespace) -> int:
    log.info(
        "watcher_starting project=%s orchestrator=%s dry_run=%s",
        args.project, args.orchestrator, args.dry_run,
    )
    seen = 0
    emitted = 0
    for line in _event_source(args):
        event = parse_event(line)
        if event is None:
            continue
        if not is_compose_managed_oom(event, args.project):
            continue
        seen += 1
        argv = build_emit_argv(event, args.orchestrator)
        log.info("oom_observed container=%s",
                 (event.get("Actor") or {}).get("Attributes", {}).get("name"))
        if args.dry_run:
            sys.stdout.write(json.dumps({"would_emit": argv}) + "\n")
            sys.stdout.flush()
            emitted += 1
            continue
        if _run_emit_with_retry(argv):
            emitted += 1
        if args.test_event is not None:
            break
    log.info("watcher_exit seen=%d emitted=%d", seen, emitted)
    return 0


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="oom_watcher",
        description="Emit alerts on docker OOM events for scaffold-engine containers.",
    )
    p.add_argument("--project", default=DEFAULT_PROJECT,
                   help="compose project label to filter on")
    p.add_argument("--orchestrator", default=DEFAULT_ORCHESTRATOR,
                   help="container name to docker-exec for emit")
    p.add_argument("--dry-run", action="store_true",
                   help="print the emit argv instead of executing it")
    p.add_argument("--stdin", action="store_true",
                   help="read JSON event lines from stdin instead of docker events")
    p.add_argument("--test-event", default=None,
                   help="process exactly one JSON event line and exit (for tests)")
    p.add_argument("--log-level", default="INFO",
                   help="logging level (DEBUG / INFO / WARNING / ERROR)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    try:
        return run(args)
    except KeyboardInterrupt:
        log.info("watcher_interrupted")
        return 0


if __name__ == "__main__":
    sys.exit(main())
