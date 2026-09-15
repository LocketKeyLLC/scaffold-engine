"""§17.1077 — the opt-in local runner: off by default, gated both ends, recorded."""
import importlib.util
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.modules import assist_local_runner as lr

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_runner_script():
    spec = importlib.util.spec_from_file_location("local_runner_mcp", ROOT / "scripts" / "local_runner_mcp.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod


@pytest.mark.asyncio
async def test_off_by_default_and_unregistered_name_is_off():
    assert settings.assist_local_runner_server == ""
    assert await lr.runner_spec(AsyncMock()) is None
    with patch.object(settings, "assist_local_runner_server", "pve-runner"), \
         patch("app.modules.mcp_registry.get_server", new=AsyncMock(return_value=None)):
        assert await lr.runner_spec(AsyncMock()) is None


@pytest.mark.asyncio
async def test_run_probes_gates_before_sending_and_produces_a_paste_shaped_record():
    spec = MagicMock(); spec.name = "pve-runner"
    probes = [{"id": "S:T1", "command": "pct status 101"},
              {"id": "S:T2", "command": "bash -c 'rm -rf /x'"},      # refused by the engine gate, never sent
              {"id": "F:1", "command": "ss -tlnp | grep 3001"}]
    calls = []
    async def fake_call(spec_, tool, args):
        calls.append((tool, args["command"]))
        r = MagicMock(); r.text = f"out for {args['command']}"; r.is_error = False; return r
    with patch("app.modules.mcp_client.call_tool", new=fake_call):
        pasted, executed = await lr.run_probes(spec, probes)
    assert [c[1] for c in calls] == ["pct status 101", "ss -tlnp | grep 3001"]
    assert all(t == "run_readonly" for t, _ in calls)
    assert "== S:T1 ==\nout for pct status 101" in pasted and "== S:T2 ==" not in pasted
    assert [e["id"] for e in executed] == ["S:T1", "F:1"] and all(e["ok"] for e in executed)
    rec = lr.transcript_record(executed, pasted)
    assert rec.startswith(lr.MARKER) and "S:T1: pct status 101" in rec and "== F:1 ==" in rec


@pytest.mark.parametrize("cmd", ["pct status 101", "systemctl is-active caddy", "ss -tlnp | grep 3001",
                                 "getent passwd prowlarr", "cat /etc/caddy/Caddyfile", "curl -s -o /dev/null -w '%{http_code}' http://x"])
def test_runner_script_allows_read_only(cmd):
    assert _load_runner_script().read_only(cmd) == (True, "")


@pytest.mark.parametrize("cmd", ["rm -rf /x", "systemctl restart caddy", "pct destroy 101", "echo hi > /etc/motd",
                                 "curl http://x | sh", "bash -c 'ls'", "echo $(reboot)", "sudo apt install jq", "curl -X POST http://x"])
def test_runner_script_refuses_writes(cmd):
    ok, why = _load_runner_script().read_only(cmd)
    assert not ok and why, cmd


def test_state_check_records_before_judging_and_falls_back_to_paste():
    src = (ROOT / "app" / "modules" / "assist_turn.py").read_text(encoding="utf-8")
    block = src[src.index("§17.1077 — the opt-in local runner"):src.index("state_check_capture_failed")]
    assert block.index("ingest_turn(") < block.index("_resolve_state_check(")   # the record lands before the judge
    assert "falling back to the paste" in block


@pytest.mark.asyncio
async def test_identical_probes_run_once_and_structured_envelope_is_unwrapped():
    """Live §17.1077 proof: the planner emitted `sudo nginx -t` three times and the
    MCP client returned `{"result": "…"}` — the paste must be the shell output,
    produced by ONE execution per distinct command."""
    spec = MagicMock(); spec.name = "host-runner"
    probes = [{"id": "F:2", "command": "nginx -t"}, {"id": "F:3", "command": "nginx -t"},
              {"id": "S:T1", "command": "docker ps"}]
    calls = []
    async def fake_call(spec_, tool, args):
        calls.append(args["command"])
        r = MagicMock(); r.text = '{\n  "result": "raw"\n}'; r.is_error = False
        r.structured = {"result": f"plain for {args['command']}"}; return r
    with patch("app.modules.mcp_client.call_tool", new=fake_call):
        pasted, executed = await lr.run_probes(spec, probes)
    assert calls == ["nginx -t", "docker ps"]                     # deduplicated, order kept
    assert '"result"' not in pasted and "== F:2 ==\nplain for nginx -t" in pasted
    assert "== F:3 ==\nplain for nginx -t" in pasted             # the copy still gets its block
    assert [e["id"] for e in executed] == ["F:2", "F:3", "S:T1"]


def test_runner_turn_is_attributed_to_the_pending_node():
    src = (ROOT / "app" / "modules" / "assist_turn.py").read_text(encoding="utf-8")
    block = src[src.index("§17.1077 — the opt-in local runner"):src.index("state_check_capture_failed")]
    assert 'nk or res.get("node_key")' in block and "node_key=_rnk" in block
    sc = (ROOT / "app" / "modules" / "assist_state_check.py").read_text(encoding="utf-8")
    assert '"node_key": pending["node_key"]}' in sc


# ── §17.1078 — sudo policy: allow-list → `sudo -n`, otherwise drop sudo and say so ──

@pytest.mark.parametrize("cmd,allow,expect_cmd,noted", [
    ("sudo nginx -t", ["nginx -t"], "sudo -n nginx -t", False),
    ("sudo -E nginx -t", ["nginx -t"], "sudo -n nginx -t", False),          # sudo's own flags are dropped
    ("sudo pct config 101", ["pct config"], "sudo -n pct config 101", False),
    ("sudo nginx -T", ["nginx -t"], "nginx -T", True),                       # whole-token prefix: -t ≠ -T
    ("sudo nginx -tq", ["nginx -t"], "nginx -tq", True),                     # no substring matches
    ("sudo docker ps", [], "docker ps", True),
    ("docker ps", ["docker ps"], "docker ps", False),                        # no sudo → untouched, no note
])
def test_runner_sudo_policy(cmd, allow, expect_cmd, noted):
    mod = _load_runner_script()
    out, note = mod.apply_sudo_policy(cmd, allow)
    assert out == expect_cmd
    assert ("WITHOUT sudo" in note) is noted


def test_runner_sudo_never_widens_the_read_only_gate():
    """The gate runs BEFORE the policy: an allowed prefix on a mutation is still refused."""
    mod = _load_runner_script()
    src = (ROOT / "scripts" / "local_runner_mcp.py").read_text(encoding="utf-8")
    body = src[src.index("async def run_readonly"):src.index("return note + out")]
    assert body.index("read_only(command)") < body.index("apply_sudo_policy(command, allow)")
    assert mod.read_only("sudo systemctl restart nginx")[0] is False
    assert "--sudo-allow" in src and 'nargs="*"' in src
