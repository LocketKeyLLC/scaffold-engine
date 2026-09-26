"""§17.1052 — deterministic "this worked" facts from the operator's pastes
(app/modules/assist_memory.worked_command_facts).

Live: `node server.js` printed "Backend API running on port 3001" at 12:24 and
the model-derived memory recorded the failures around it; six hours later the
fix loop chased `index.js` from the package file.
"""
from __future__ import annotations

import pytest

import app.modules.execution_agent  # noqa: F401 — load-bearing (real app.database)
from app.modules.assist_memory import worked_command_facts

PASTE = """root@pve:~# pct exec 111 -- bash -c 'cd /opt/control-panel-backend && node server.js'
Backend API running on port 3001
^Croot@pve:~# pct exec 111 -- cat /var/log/control-panel.log
cat: /var/log/control-panel.log: No such file or directory
root@pve:~# pct exec 111 -- systemd-run --unit=control-panel --working-directory=/opt/control-panel-backend node server.js
Running as unit: control-panel.service
root@pve:~# pct exec 111 -- systemctl is-active control-panel
active
root@pve:~# pct start 120
root@pve:~# qm status 110
status: stopped
"""


def test_commands_followed_by_success_output_become_facts_and_failures_do_not():
    facts = worked_command_facts(PASTE, node_key="T37")
    assert facts[0].startswith("Worked at T37: `pct exec 111 -- bash -c 'cd /opt/control-panel-backend && node server.js'` → Backend API running on port 3001")
    assert any("systemd-run --unit=control-panel" in f and "Running as unit" in f for f in facts)
    assert any("systemctl is-active control-panel` → active" in f for f in facts)
    joined = "\n".join(facts)
    assert "cat /var/log" not in joined          # a read command
    assert "pct start 120" not in joined         # silence is not success
    assert "qm status 110" not in joined         # "stopped" is a failure word


def test_no_prompt_lines_means_no_facts():
    assert worked_command_facts("just some prose about the build") == []
    assert worked_command_facts("") == []


def test_both_distillers_fold_in_the_worked_facts():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1] / "app/modules/assist_memory.py").read_text()
    turn = src[src.index("async def derive_turn_memory("):]
    assert "worked_command_facts(msg, node_key=node_key)" in turn
    sub = src[src.index("async def capture_session_facts("):src.index("async def derive_turn_memory(")] if src.index("async def capture_session_facts(") < src.index("async def derive_turn_memory(") else src[src.index("async def capture_session_facts("):]
    assert "worked_command_facts(evidence, node_key=node_key)" in sub


# ── §17.1174 — "ls" had no trailing space, so every ls* command was dropped ──

@pytest.mark.parametrize("cmd,out", [
    ("lsof -i :3001", "node 1234 root 20u IPv4 TCP *:3001 (LISTEN)"),
    ("lsblk -f /dev/sda", "sda1 ext4 rootfs active"),
    ("lspci -nn | grep -i nvidia", "02:00.0 NVIDIA Tesla P40 - device is up 1"),
])
def test_ls_prefixed_discovery_commands_are_remembered(cmd, out):
    """The skip list read `("echo ", "cat ", "ls", "cd ", "grep ", …)` — every
    entry but `ls` carried a trailing space, so `lsblk`, `lspci` and `lsof`
    matched `startswith` and were thrown away. Those are exactly the discovery
    commands §17.1052 added this function to remember: "a command the operator
    ran that visibly WORKED is the most durable fact a paste can carry"."""
    facts = worked_command_facts(f"root@pve:~# {cmd}\n{out}")
    assert facts and cmd in facts[0], (cmd, facts)


@pytest.mark.parametrize("cmd,out", [
    ("ls -la /etc", "total 48"),
    ("ls /opt", "app  data"),
    ("cat /etc/hostname", "pve is up 1"),
    ("echo hello", "hello is running"),
])
def test_the_genuinely_uninteresting_commands_are_still_skipped(cmd, out):
    """The skip list exists for a reason — a bare listing is not a durable
    fact. Widening `ls` to `ls ` must not widen it to `ls`-anything."""
    assert worked_command_facts(f"root@pve:~# {cmd}\n{out}") == []
