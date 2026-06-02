#!/usr/bin/env python3
"""§17.387 — host-side kernel-OOM event watcher for scaffold-engine.

Streams ``journalctl -kf`` and emits a ``host.oom_killed`` alert via the
orchestrator's ``app.observability.alerts`` CLI for every HOST-scope
kernel OOM kill — i.e., a kill triggered by global memory pressure, not
by a docker container hitting its ``mem_limit``.

This is the dmesg-coverage counterpart to §17.161, which watches
``docker events --filter event=oom`` and emits ``container.oom_killed``
for cgroup-scoped OOMs. The two watchers are complementary and dedup
naturally by alert kind:

  * Container OOM (mem_limit breach)  → §17.161  → ``container.oom_killed``
  * Host OOM      (global pressure)   → §17.387  → ``host.oom_killed``

The kernel distinguishes them in the OOM-kill message itself:

  * Host OOM   → ``Out of memory: Killed process <pid> (<comm>) ...``
  * Cgroup OOM → ``Memory cgroup out of memory: Killed process <pid> (<comm>) ...``

§17.387 watches only the host-OOM line shape; cgroup OOMs are skipped
because §17.161 already emits an alert for them via the docker-events
path. Skipping at parse time is simpler and more reliable than
dedup'ing after the fact.

Operator install:

    sudo cp scripts/scaffold-host-oom-watcher.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now scaffold-host-oom-watcher

Test event injection (no journalctl needed):

    python3 scripts/host_oom_watcher.py --test-event \
        'Out of memory: Killed process 1234 (python) total-vm:1234kB ...'

The ``--dry-run`` flag prints the emit argv instead of executing it, so
the watcher can be inspected without writing to ``system_alerts``.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import re
import subprocess
import sys
import time
from typing import Iterable, Optional

DEFAULT_ORCHESTRATOR = "scaffold-orchestrator"
ALERT_KIND = "host.oom_killed"
ALERT_SEVERITY = "critical"
EMIT_MAX_RETRIES = 4
EMIT_BACKOFF_BASE_S = 1.0
EMIT_BACKOFF_CAP_S = 30.0

# The host-OOM kill notification. Kernel 5.x+ format (also 4.x). The
# leading anchor matches the *start* of the kernel line; the
# `Memory cgroup` prefix that signals a cgroup-scoped OOM does NOT match
# because the regex is anchored. Match group 1 is the PID, group 2 is
# the process comm (kernel-truncated to TASK_COMM_LEN=16 chars).
_HOST_OOM_RE = re.compile(
    r"^Out of memory: Killed process (?P<pid>\d+) \((?P<comm>[^)]+)\)"
)

# Cgroup-OOM detection — same kill but emitted by mem cgroup OOM-killer.
# Skipping at parse time keeps the §17.161 vs §17.387 boundary clean.
_CGROUP_OOM_PREFIX = "Memory cgroup out of memory:"

log = logging.getLogger("host_oom_watcher")


def parse_oom_line(line: str) -> Optional[dict]:
    """Parse one journalctl kernel line; return ``{pid, comm}`` for a
    HOST-scope OOM kill, or None for any other line shape.

    Returns None (skip) for:

      * Empty lines / non-OOM kernel chatter.
      * Cgroup-scoped OOM lines (``Memory cgroup out of memory: ...``)
        — those are §17.161's job.
      * The pre-kill diagnostic lines (``invoked oom-killer:``,
        ``oom-kill:constraint=...``, etc.) — we emit only on the kill
        notification, not the buildup.

    Returns ``{"pid": int, "comm": str}`` for a host OOM kill we should
    alert on.
    """
    s = line.strip()
    if not s:
        return None
    # Defensive: also accept lines with a kernel-prefix that journalctl
    # might emit when not using ``-o cat`` — e.g. ``kernel: Out of
    # memory: ...``. Strip a single ``kernel: `` prefix if present.
    if s.startswith("kernel: "):
        s = s[len("kernel: "):]
    if s.startswith(_CGROUP_OOM_PREFIX):
        return None
    m = _HOST_OOM_RE.match(s)
    if not m:
        return None
    try:
        return {"pid": int(m.group("pid")), "comm": m.group("comm")}
    except (ValueError, KeyError):
        return None


def _now_iso() -> str:
    """UTC ISO-8601 timestamp for the event. The kernel includes its own
    monotonic timestamp in the line, but converting that to wall time
    requires reading the boot time; the alert layer only needs a UTC
    wall timestamp for ``created_at``, which is wall time anyway."""
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def build_emit_argv(event: dict, orchestrator: str) -> list[str]:
    """Compose the ``docker exec ... python -m app.observability.alerts emit`` argv.

    Dedup key is ``host.oom_killed:<comm>`` so repeated host OOMs of the
    SAME process comm are rate-limited by ``alert_cooldown_seconds``
    (default 1 h). This mirrors §17.161's per-container dedup; the comm
    is the closest analog of "what was killed" available in the kernel
    line.
    """
    pid = event["pid"]
    comm = event["comm"]
    payload = {
        "comm": comm,
        "pid": pid,
        "event_time_utc": _now_iso(),
    }
    message = (
        f"Host kernel OOM-killed process {comm} (pid {pid}) — global "
        "memory pressure on the host, NOT a per-container mem_limit "
        "breach. Inspect host free memory, swap, and process memory "
        "usage; consider lowering compose mem_limit caps or raising "
        "physical memory."
    )
    return [
        "docker", "exec", orchestrator,
        "python", "-m", "app.observability.alerts", "emit",
        "--kind", ALERT_KIND,
        "--severity", ALERT_SEVERITY,
        "--message", message,
        "--payload", json.dumps(payload, separators=(",", ":")),
        "--dedup-key", f"{ALERT_KIND}:{comm}",
    ]


def _run_emit_with_retry(argv: list[str]) -> bool:
    """Run the emit argv with bounded exponential backoff.

    Returns True if the emit succeeded (exit 0). Returns False if every
    retry failed — the orchestrator may itself be down (it was the host
    OOM victim and hasn't restarted yet). Logs to stderr via the module
    logger; never raises.
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
    """Yield kernel-log line strings from the configured source.

    Three modes:

      * ``--test-event '<line>'`` — yield that one line and stop. Used by tests.
      * ``--stdin`` — read lines from stdin (one kernel line per line).
      * default — exec ``journalctl -kf --since=now -o cat`` and stream
        stdout. ``--since=now`` ensures the watcher does NOT re-emit
        historical OOMs on startup (each restart processes only new
        events); ``-o cat`` strips the systemd-journal timestamp/host
        prefix so the parser sees the raw kernel line.
    """
    if args.test_event is not None:
        yield args.test_event
        return
    if args.stdin:
        for line in sys.stdin:
            yield line
        return
    cmd = [
        "journalctl",
        "-k",  # kernel messages only
        "-f",  # follow (stream new lines)
        "--since=now",  # don't replay historical OOMs on watcher restart
        "-o", "cat",   # strip timestamp/host/kernel: prefix
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
        "watcher_starting orchestrator=%s dry_run=%s",
        args.orchestrator, args.dry_run,
    )
    seen = 0
    emitted = 0
    for line in _event_source(args):
        event = parse_oom_line(line)
        if event is None:
            continue
        seen += 1
        argv = build_emit_argv(event, args.orchestrator)
        log.info("host_oom_observed pid=%d comm=%s", event["pid"], event["comm"])
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
        prog="host_oom_watcher",
        description="Emit alerts on kernel-level (host-scope) OOM events.",
    )
    p.add_argument("--orchestrator", default=DEFAULT_ORCHESTRATOR,
                   help="container name to docker-exec for emit")
    p.add_argument("--dry-run", action="store_true",
                   help="print the emit argv instead of executing it")
    p.add_argument("--stdin", action="store_true",
                   help="read kernel lines from stdin instead of journalctl")
    p.add_argument("--test-event", default=None,
                   help="process exactly one kernel line and exit (for tests)")
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
