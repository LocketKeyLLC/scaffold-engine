"""§17.1470 — the checks a step names after "Done when" always run, whatever the drafted block chose.

Live (2026-10-11): ADR1 (fixture) named three greps that must print nothing — in VM 106, in the panel's LXC 111
and on the host. The drafted verify list (below, verbatim) kept only the host grep and added `systemctl is-active`
reads; all four confirmed, ADR1 was recorded "already met", and the panel still reached Palworld at 192.168.1.106.
"""
import asyncio
import json
import pathlib

from app.modules import supervised_runs as sr

NODE = json.loads(pathlib.Path("tests/fixtures/adr1_node_2026_10_11.json").read_text())
DRAFTED = ["grep -rIl -e '192.168.1.106' /etc/pve/firewall /etc/hosts 2>/dev/null; true",
           "pct exec 111 -- systemctl is-active control-panel.service",
           "qm guest exec 106 -- systemctl is-active palworld.service",
           "qm guest exec 106 -- ss -tlnp | grep 38477"]


def test_the_steps_greps_join_the_drafted_checks():
    out = sr.with_own_checks(NODE, DRAFTED)
    assert out[:4] == DRAFTED and len(out) == 6
    assert any(c.startswith("pct exec 111 -- sh -c \"grep -rIl") for c in out[4:])
    assert any(c.startswith("qm guest exec 106 -- sh -c \"grep -rIl") for c in out[4:])


def test_already_met_reads_the_steps_own_checks(monkeypatch):
    seen = {}

    async def run_verify(spec, cmds, env):
        seen["cmds"] = list(cmds)
        # the panel grep finds the stale file; nothing else does
        report = "\n".join(f"$ {c}\n" + ("/opt/control-panel-backend/palworld_settings_route.js" if "pct exec 111 -- sh -c" in c
                                        else "active") for c in cmds)
        return report, [{"id": f"V{i}", "ran": True} for i, _ in enumerate(cmds, 1)]

    async def verdicts(claim, cmds, pasted, expects=None):
        return [{"id": f"V{i}", "verdict": "contradicted" if "pct exec 111 -- sh -c" in c else "confirmed"}
                for i, c in enumerate(cmds, 1)]
    monkeypatch.setattr(sr, "run_verify", run_verify)
    monkeypatch.setattr(sr, "_verify_verdicts", verdicts)
    monkeypatch.setattr(sr, "checks_can_witness_the_change", lambda *a, **k: "")
    frame = {"verify": DRAFTED, "commands": ["pct exec 111 -- sed -i s/a/b/ /opt/x"], "runbook": ""}
    assert asyncio.run(sr.already_met(object(), NODE, frame, {})) is None
    assert len(seen["cmds"]) == 6


def test_the_run_verifies_with_them_too():
    src = pathlib.Path("app/modules/supervised_runs.py").read_text()
    assert "verify_cmds = with_own_checks({\"title\": waiting.get(\"title\"), \"description\": waiting.get(\"description\")}," in src
    assert "verify_cmds = with_own_checks(node, (frame or {}).get(\"verify\") or [])" in src
