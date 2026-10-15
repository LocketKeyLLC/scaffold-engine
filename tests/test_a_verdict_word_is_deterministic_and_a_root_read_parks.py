r"""§17.1574 — three seams the RA job (9e1c2c68) and the panel job (c2792d4b) exposed on 2026-10-15.

1574a  A check that ends with its own verdict word (`STATE-OK` / `STATE-WRONG`, §17.1570g) is judged on that word,
       deterministically: live, T3–T7's checks ended STATE-WRONG (unprivileged `nft list`) and the model judge still
       read the counter lines above as "confirms the goal" — five steps recorded already met over their own verdict.
1574b  A run_readonly MCP node the runner refuses for PRIVILEGE (`pve-firewall status` → ipcc_send_rec failed) is a
       read the operator may approve on the root channel, not a failed step that ends the job: it is marked
       `needs_root`, put back to pending, and reported needs_approval; the frontier then holds it.
1574c  "Probe each endpoint authenticated" presents the login the engine holds (§17.1548's logins) to every GET
       route the service's own code registers; the draft guessed routes and sent a cookie nobody holds.
"""
from __future__ import annotations

import json
import subprocess
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.modules import supervised_runs as sr


# ── 1574a ─────────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_an_absent_verdict_word_contradicts_without_a_judge(monkeypatch):
    from app.modules import assist_state_check as asc
    from app import model_router
    called = []

    async def _no_judge(*a, **k):
        called.append(1)
        raise RuntimeError("the judge must not be asked")
    monkeypatch.setattr(model_router, "tool_call", _no_judge)
    chk = "nft list table bridge x; systemctl is-active x && echo STATE-OK || echo STATE-WRONG"
    probes = [{"id": "V:1", "kind": "state", "claim": "Verify packet counter increments", "command": chk, "expect": "STATE-OK"}]
    pasted = "== V:1 ==\ncounter packets 12 bytes 2784 drop\nactive\nnetlink: Error: cache initialization failed: Operation not permitted\nSTATE-WRONG\n"
    out = await asc.judge_outputs(probes, pasted)
    assert out[0]["verdict"] == "contradicted" and "STATE-OK" in out[0]["reason"] and "STATE-WRONG" in out[0]["reason"]
    assert not called
    ok = await asc.judge_outputs(probes, "== V:1 ==\nactive\nSTATE-OK\n")
    assert ok[0]["verdict"] == "confirmed"
    # §17.1580 — the run path's record opens each section `$ <command>`; the command's own `echo STATE-OK` is not output
    echoed = f"== V:1 ==\n$ {chk}\ncounter packets 12\nactive\nSTATE-WRONG\n"
    bad = await asc.judge_outputs(probes, echoed)
    assert bad[0]["verdict"] == "contradicted" and "STATE-WRONG" in bad[0]["reason"], bad[0]
    good = await asc.judge_outputs(probes, f"== V:1 ==\n$ {chk}\nactive\nSTATE-OK\n")
    assert good[0]["verdict"] == "confirmed"
    # a prose expectation still goes to the judge path (no deterministic contradiction)
    prose = [{"id": "V:1", "kind": "state", "claim": "c", "command": "cat /etc/x", "expect": "the file holds the key"}]
    res = await asc.judge_outputs(prose, "== V:1 ==\nsomething else\n")
    assert called and "verdict word" not in str(res[0].get("reason"))
    # a single lowercase token ("running") is not a verdict word: negative evidence or the judge decide, as before
    lower = [{"id": "V:1", "kind": "state", "claim": "container 111 running", "command": "pct status 111", "expect": "running"}]
    res2 = await asc.judge_outputs(lower, "== V:1 ==\nstatus: stopped\n")
    assert "verdict word" not in str(res2[0].get("reason"))


# ── 1574b ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def _cfg(**over):
    base = {"server": "pve-runner", "tool": "run_readonly", "args": {"command": "pve-firewall status; pve-firewall compile"}}
    base.update(over)
    return base


def test_a_root_marked_read_is_held_by_the_frontier():
    node = {"node_key": "T8", "title": "Verify pve-firewall untouched", "tool": "MCP", "tool_config": json.dumps(_cfg(needs_root=True))}
    assert sr.mcp_needs_the_pause(node) and sr.is_hands_on_node(node)
    rb = sr.mcp_write_runbook(node, {}, {})
    assert rb and "pve-firewall status; pve-firewall compile" in rb
    plain = {"node_key": "T8", "title": "Verify pve-firewall untouched", "tool": "MCP", "tool_config": json.dumps(_cfg())}
    assert not sr.mcp_needs_the_pause(plain)


@pytest.mark.asyncio
async def test_execute_mcp_node_parks_a_read_the_runner_refused_for_privilege(monkeypatch):
    from app.modules import mcp_node, mcp_client, mcp_registry
    spec = MagicMock(enabled=True); spec.name = "pve-runner"
    monkeypatch.setattr(mcp_registry, "get_server", AsyncMock(return_value=spec))
    refusal = ("(the runner is UNPRIVILEGED and this command needs root — the output below is a permission refusal, "
               "not a statement about your system.)\nStatus: enabled/running\nipcc_send_rec[1] failed: Unknown error -1\n")
    res = MagicMock(text=refusal, structured={"result": refusal}, is_error=False)
    monkeypatch.setattr(mcp_client, "call_tool", AsyncMock(return_value=res))
    writes = []

    async def _exec(stmt, params=None):
        writes.append((str(stmt), params or {}))
        r = MagicMock(); r.fetchone = MagicMock(return_value=("id",)); r.scalar = MagicMock(return_value=None)
        return r

    db = MagicMock(); db.execute = AsyncMock(side_effect=_exec); db.commit = AsyncMock()
    node = {"id": "nid", "node_key": "T8", "title": "Verify pve-firewall untouched", "tool": "MCP", "tool_config": json.dumps(_cfg())}
    out = await mcp_node.execute_mcp_node(db, node=node, upstream_outputs={}, brief={}, job_id="job")
    assert out["status"] == "needs_approval" and "root" in out["reason"]
    upd = [p for q, p in writes if "needs_root" in str(p.get("cfg", "")) and "status = 'pending'" in q]
    assert upd and json.loads(upd[0]["cfg"])["needs_root"] is True
    assert not any("status = 'failed'" in q for q, _ in writes)


# ── 1574c ─────────────────────────────────────────────────────────────────────────────────────────────────────────
class _Svc:
    def __init__(self, name, guest, ports, unit=""):
        self.name, self.guest, self.ports, self.unit, self.vm = name, guest, ports, unit, False


SERVICES = [_Svc("control-panel", "111", ("3001",), "control-panel.service")]
INV = {"names": {"111": "control-panel"}}
LOGINS = [{"name": "PANEL_PASSWORD", "user": "panel", "hint": "the control panel's login password (user `panel`)"}]
T2 = {"node_key": "T2", "title": "Probe each endpoint authenticated", "tool": "Shell",
      "description": "Call every /api/* endpoint of the control panel on container 111 authenticated as the panel user; "
                     "record HTTP status and body shape."}


def test_the_probe_presents_the_held_login_to_every_registered_get_route():
    cmds, verify, notes = sr.rewrite_probe_service_endpoints(T2, ["<model>"], [], SERVICES, INV, logins=LOGINS)
    assert notes and "§17.1574c" in notes[0]["why"] and cmds == verify and len(cmds) == 1
    c = cmds[0]
    assert c.startswith("pct exec 111 -- sh -c '") and c.endswith(' <<< "<PANEL_PASSWORD>"')
    assert "cpw=$(cat); " in c and '-u "panel:$cpw"' in c and "port=3001; " in c and "127.0.0.1:$port$ep" in c
    assert "/opt/control*" in c and '--exclude-dir="*.bak*"' in c and "HTTP %{http_code} in %{time_total}s" in c
    assert "'\\''" not in c                                                 # no quote-inside-quote escapes
    assert " -o " not in c and "PANEL_SESSION_COOKIE" not in c and "<PANEL_PASSWORD>" not in c.split("<<<")[0]
    assert c.split("<<<")[0].rstrip().rstrip("'").endswith('echo "probed:$n"; test -n "$port" && test "$n" -gt 0 && echo STATE-OK || echo STATE-WRONG')
    assert 'test -z "$fails"' not in c and "unanswered:$fails" in c       # §17.1580 — a hanging route is the finding, not the failure
    assert notes[0]["expect"] == {c: "STATE-OK"}
    assert subprocess.run(["bash", "-n", "-c", c], capture_output=True, text=True).returncode == 0


def test_the_probe_is_a_read_the_read_only_channel_takes():
    from app.modules.assist_state_check import read_only_command
    cmds, _, _ = sr.rewrite_probe_service_endpoints(T2, ["x"], [], SERVICES, INV, logins=LOGINS)
    assert read_only_command(cmds[0])


def test_without_a_measured_service_the_port_is_read_off_the_code_in_the_guest():
    # §17.1574d — live (c2792d4b): nothing was measured for 111, yet the login names the guest ("panel" in control-panel)
    cmds, verify, notes = sr.rewrite_probe_service_endpoints(T2, ["x"], [], [], INV, logins=LOGINS)
    assert notes and cmds == verify and cmds[0].startswith("pct exec 111 -- sh -c '") and cmds[0].endswith(' <<< "<PANEL_PASSWORD>"')
    c = cmds[0]
    # §17.1579 — what LISTENS in the guest first (live: server.js has no literal; `ss` says node on 3001), then the code
    assert 'port=$(ss -ltnpH 2>/dev/null | grep -E "users:...(node|python|' in c and "127.0.0.1:$port$ep" in c
    assert 'test -n "$port" || port=$(grep -rhoE "listen\\(\\s*[0-9]+" /opt/control*' in c
    assert c.index("ss -ltnpH") < c.index('grep -rhoE "listen') and 'test -n "$port" || echo "FAILED: no app listener' in c
    assert "'\\''" not in c
    assert subprocess.run(["bash", "-n", "-c", c], capture_output=True, text=True).returncode == 0
    from app.modules.assist_state_check import read_only_command
    assert read_only_command(c)


def test_the_probe_needs_a_held_login_for_that_service_and_the_right_words():
    assert not sr.rewrite_probe_service_endpoints(T2, ["x"], [], SERVICES, INV, logins=[])[2]
    other = [{"name": "JELLYFIN_PASSWORD", "user": "admin", "hint": "jellyfin login"}]
    assert not sr.rewrite_probe_service_endpoints(T2, ["x"], [], SERVICES, INV, logins=other)[2]
    unauth = dict(T2, title="Probe each endpoint", description="Call every /api/* endpoint the control panel on container 111 serves")
    assert not sr.rewrite_probe_service_endpoints(unauth, ["x"], [], SERVICES, INV, logins=LOGINS)[2]
    fix = {"title": "Fix Radarr endpoint backend", "tool": "Shell", "description": "make /api/media-library return data, authenticated"}
    assert not sr.rewrite_probe_service_endpoints(fix, ["x"], [], SERVICES, INV, logins=LOGINS)[2]


def test_frame_run_runs_the_probe_rewrite_with_the_held_logins_before_the_key_reader():
    import inspect
    src = inspect.getsource(sr)
    assert 'rewrite_probe_service_endpoints(\n        node, cmds, verify, services, inventory, logins=(policy or {}).get("held_logins") or [])' in src
    assert src.index("rewrite_probe_service_endpoints(\n        node, cmds") < src.index("read_on_the_machine(cmds")


# ── 1574e — the approved root run of a refused read is the reading; its refused re-check is not "unverified" ─────
def test_the_root_run_of_a_refused_read_is_the_reading():
    w = {"hands_on_reason": "mcp:needs-root", "title": "Verify pve-firewall untouched"}
    ran = [{"command": "pve-firewall status; pve-firewall compile", "output": "Status: enabled/running\nipset cmdlist: …", "exit": 0, "ok": True}]
    assert sr.the_run_is_the_read(w, ran)
    assert not sr.the_run_is_the_read(w, [{"command": "x", "output": "", "exit": 0, "ok": True}])      # printed nothing
    assert not sr.the_run_is_the_read(w, [{"command": "x", "output": "boom", "exit": 1, "ok": False}])  # failed
    assert not sr.the_run_is_the_read({"hands_on_reason": "tool:shell"}, ran)                           # a change: checks stand
    import inspect
    src = inspect.getsource(sr)
    assert "and the_run_is_the_read(waiting, executed):" in src and src.index("the_run_is_the_read(waiting, executed)") < src.index('_unverified = (')
