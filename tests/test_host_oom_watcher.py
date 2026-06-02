"""§17.387 — unit tests for scripts/host_oom_watcher.py.

Targets the pure helpers: parse_oom_line + build_emit_argv. The
journalctl loop is exercised via ``--test-event`` in an integration
smoke (out of unit-test scope).
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "host_oom_watcher",
    pathlib.Path(__file__).resolve().parents[1] / "scripts" / "host_oom_watcher.py",
)
host_oom_watcher = importlib.util.module_from_spec(_SPEC)
assert _SPEC is not None and _SPEC.loader is not None
_SPEC.loader.exec_module(host_oom_watcher)


# ---------------------------------------------------------------------------
# parse_oom_line — host vs cgroup vs noise
# ---------------------------------------------------------------------------

class TestParseOomLine:
    def test_host_oom_line_yields_pid_and_comm(self):
        line = (
            "Out of memory: Killed process 1234 (postgres) "
            "total-vm:1234kB anon-rss:567kB file-rss:0kB shmem-rss:0kB "
            "UID:999 pgtables:12kB oom_score_adj:0"
        )
        out = host_oom_watcher.parse_oom_line(line)
        assert out == {"pid": 1234, "comm": "postgres"}

    def test_cgroup_oom_line_is_skipped(self):
        """§17.161 already alerts on cgroup OOMs via docker events. §17.387
        must NOT double-emit; lines starting with the cgroup-OOM prefix
        return None and the watcher moves on."""
        line = (
            "Memory cgroup out of memory: Killed process 1234 (python) "
            "total-vm:1234kB anon-rss:567kB"
        )
        assert host_oom_watcher.parse_oom_line(line) is None

    def test_kernel_prefix_is_tolerated(self):
        """If journalctl is invoked without ``-o cat``, lines may carry a
        ``kernel: `` prefix. The parser strips that and matches anyway."""
        line = "kernel: Out of memory: Killed process 9999 (python) total-vm:1kB"
        out = host_oom_watcher.parse_oom_line(line)
        assert out == {"pid": 9999, "comm": "python"}

    def test_kernel_prefixed_cgroup_line_still_skipped(self):
        line = "kernel: Memory cgroup out of memory: Killed process 1 (init) total-vm:1kB"
        assert host_oom_watcher.parse_oom_line(line) is None

    def test_empty_line_returns_none(self):
        assert host_oom_watcher.parse_oom_line("") is None
        assert host_oom_watcher.parse_oom_line("   \n") is None

    def test_unrelated_kernel_line_returns_none(self):
        """Anything that isn't an OOM kill notification returns None."""
        for line in (
            "python invoked oom-killer: gfp_mask=0x100cca",
            "oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null),global_oom,task=python,pid=1234",
            "[task pid]   uid  tgid total_vm    rss",
            "CPU: 0 PID: 1234 Comm: python",
            "Mem-Info:",
            "Tasks state (memory values in pages):",
            "Hardware name: Pop!_OS",
        ):
            assert host_oom_watcher.parse_oom_line(line) is None, f"matched: {line!r}"

    def test_comm_with_special_chars(self):
        """Kernel comm field can contain spaces, dashes, etc. — kernel
        truncates to TASK_COMM_LEN=16 chars. The regex matches anything
        up to the closing paren."""
        line = "Out of memory: Killed process 555 (kworker/u8:0-events) total-vm:1kB"
        out = host_oom_watcher.parse_oom_line(line)
        assert out == {"pid": 555, "comm": "kworker/u8:0-events"}

    def test_truncated_oom_line_without_extras_still_parses(self):
        """Minimal line shape — pid + comm only — still parses. The
        kernel always emits the extras (``total-vm:...``) but a future
        kernel version that doesn't shouldn't break the watcher."""
        line = "Out of memory: Killed process 42 (sh)"
        out = host_oom_watcher.parse_oom_line(line)
        assert out == {"pid": 42, "comm": "sh"}

    def test_malformed_pid_returns_none(self):
        """Defensive: a malformed line that almost looks like the
        pattern but has a non-numeric pid returns None (not crash)."""
        line = "Out of memory: Killed process FOO (python) total-vm:1kB"
        assert host_oom_watcher.parse_oom_line(line) is None


# ---------------------------------------------------------------------------
# build_emit_argv — alert CLI composition
# ---------------------------------------------------------------------------

def _flags(argv: list[str]) -> dict[str, str]:
    """Return the --flag → value mapping from the alerts-CLI tail of argv."""
    i = argv.index("emit") + 1
    tail = argv[i:]
    return dict(zip(tail[::2], tail[1::2]))


class TestBuildEmitArgv:
    def test_dispatches_to_named_orchestrator(self):
        argv = host_oom_watcher.build_emit_argv(
            {"pid": 1234, "comm": "postgres"},
            "scaffold-orchestrator",
        )
        assert argv[:5] == [
            "docker", "exec", "scaffold-orchestrator",
            "python", "-m",
        ]
        assert argv[5] == "app.observability.alerts"
        assert argv[6] == "emit"

    def test_kind_is_host_oom_killed(self):
        argv = host_oom_watcher.build_emit_argv(
            {"pid": 1, "comm": "init"},
            "scaffold-orchestrator",
        )
        assert _flags(argv)["--kind"] == "host.oom_killed"

    def test_severity_is_critical(self):
        argv = host_oom_watcher.build_emit_argv(
            {"pid": 1, "comm": "init"},
            "scaffold-orchestrator",
        )
        assert _flags(argv)["--severity"] == "critical"

    def test_dedup_key_uses_per_comm_scoping(self):
        """Repeated host OOMs of the same comm rate-limit via
        alert_cooldown_seconds (default 1h). Per-comm dedup mirrors
        §17.161's per-container dedup."""
        argv = host_oom_watcher.build_emit_argv(
            {"pid": 7, "comm": "python"},
            "scaffold-orchestrator",
        )
        assert _flags(argv)["--dedup-key"] == "host.oom_killed:python"

    def test_payload_carries_comm_pid_and_event_time(self):
        argv = host_oom_watcher.build_emit_argv(
            {"pid": 4321, "comm": "redis-server"},
            "scaffold-orchestrator",
        )
        payload = json.loads(_flags(argv)["--payload"])
        assert payload["comm"] == "redis-server"
        assert payload["pid"] == 4321
        # ISO-8601 UTC timestamp; we don't assert exact value (wall clock)
        # but it should be non-empty and parse roundtrip-safely.
        from datetime import datetime
        ts = payload["event_time_utc"]
        assert ts.endswith("+00:00") or ts.endswith("Z")
        # round-trip via fromisoformat (3.11+ accepts the +00:00 form)
        assert datetime.fromisoformat(ts) is not None

    def test_message_explains_host_vs_container_distinction(self):
        """Operator-facing message must say 'host' OOM, NOT
        'container' — and must name the comm + pid so the receiver
        can grep journalctl for context without an extra hop."""
        argv = host_oom_watcher.build_emit_argv(
            {"pid": 1234, "comm": "ngspice"},
            "scaffold-orchestrator",
        )
        msg = _flags(argv)["--message"]
        assert "host" in msg.lower()
        assert "ngspice" in msg
        assert "1234" in msg
        # Specifically NOT a per-container mem_limit message — this
        # alert kind is about HOST pressure, not container caps.
        assert "global memory pressure" in msg.lower() or "host" in msg.lower()
