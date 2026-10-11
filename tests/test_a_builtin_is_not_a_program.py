"""§17.1475 — a shell builtin is not a program: the runner runs a check as `sudo <command>`.

Live (ADD155, 2026-10-11): `command -v ipmitool` and `command -v ras-mc-ctl` answered "sudo: command: command not
found" with /usr/bin/ipmitool and /usr/sbin/ras-mc-ctl installed; the step was failed "the work did not land".
"""
import asyncio

import pytest

from app.modules import mcp_client as mc


@pytest.mark.parametrize("cmd,want", [
    ("command -v ipmitool", "which ipmitool"),
    ("command -v ras-mc-ctl", "which ras-mc-ctl"),
    ("type caddy", "which caddy"),
    ("hash node", "which node"),
    ("command -v ipmitool && systemctl is-active rasdaemon", "which ipmitool && systemctl is-active rasdaemon"),
    ("systemctl is-active rasdaemon", "systemctl is-active rasdaemon"),
    ("pct exec 111 -- sh -c 'command -v node'", "pct exec 111 -- sh -c 'command -v node'"),   # inside a shell: fine
    ("grep 'command -v' /etc/x", "grep 'command -v' /etc/x"),
])
def test_lookups_become_which(cmd, want):
    assert mc.without_lookup_builtins(cmd) == want


def test_call_tool_sends_the_program(monkeypatch):
    sent = {}

    class S:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def call_tool(self, name, args):
            sent.update(args)

            class R:
                content, isError = [], False
            return R()
    monkeypatch.setattr(mc, "_open_session", lambda spec: S())

    class Spec:
        name = "pve-runner"
    asyncio.run(mc.call_tool(Spec(), "run_readonly", {"command": "command -v ipmitool", "timeout_s": 20}))
    assert sent["command"] == "which ipmitool"
