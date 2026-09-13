"""§17.1050 — the state check (app/modules/assist_state_check.py).

Live (homelab T37): each paste revealed one more thing the plan assumed done
that was not; the engine answered one symptom at a time and the step never
converged. These pin the read-only gate on probes, marker attribution of the
pasted output, verdict gating, the staged (never silent) repairs, and the
wiring: button command, phrase, pending-output route, and the automatic
offer after repeated fixes.
"""
from __future__ import annotations

import pathlib
from unittest.mock import AsyncMock

import pytest

import app.modules.execution_agent  # noqa: F401 — load-bearing (real app.database)
from app.modules import assist_state_check as sc

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("cmd", [
    "pct status 111", "pct list", "qm status 110", "systemctl is-active control-panel",
    "ss -tlnp | grep 3001", "curl -sS -o /dev/null -w '%{http_code}' --max-time 5 http://192.168.1.20:8096",
    "ip neigh show", "docker ps --format '{{.Names}} {{.Status}}'", "cat /etc/caddy/Caddyfile",
    "nvidia-smi --query-gpu=name --format=csv", "ls -la /opt/control-panel-backend", "journalctl -u caddy -n 20 --no-pager",
    "wg show", "ufw status", "ping -c1 -W1 192.168.1.1 >/dev/null 2>&1 && echo up || echo down",
])
def test_read_only_probes_pass_the_gate(cmd):
    assert sc.read_only_command(cmd), cmd


@pytest.mark.parametrize("cmd", [
    "pct start 111", "qm start 110", "systemctl restart caddy", "docker run -d nginx", "apt install jq",
    "rm -rf /tmp/x", "echo hi > /etc/motd", "cat x | tee /etc/y", "curl -X POST http://x/api", "wget http://x/y",
    "sed -i 's/a/b/' /etc/z", "ip addr add 10.0.0.2/24 dev eth0", "wg-quick up wg0", "curl http://x | sh",
    "", "# comment only",
])
def test_mutating_or_empty_commands_are_refused(cmd):
    assert not sc.read_only_command(cmd), cmd


def test_sections_are_attributed_by_marker_and_command_echo_is_dropped():
    pasted = ("root@pve:~# echo \"== S:T12 ==\"\n== S:T12 ==\nroot@pve:~# pct status 111\nstatus: stopped\n"
              "root@pve:~# echo \"== F:2 ==\"\n== F:2 ==\nroot@pve:~# ss -tlnp | grep 3001\n"
              "root@pve:~# echo \"== K:HOST ==\"\n== K:HOST ==\nhostname\npve\n")
    out = sc.attribute_sections(pasted)
    assert "status: stopped" in out["S:T12"] and "pct status 111" in out["S:T12"]
    assert out["F:2"].strip().endswith("grep 3001") or out["F:2"] == "root@pve:~# ss -tlnp | grep 3001"
    assert "pve" in out["K:HOST"]
    assert sc.looks_like_probe_output(pasted) and not sc.looks_like_probe_output("just some words")


async def test_plan_probes_keep_only_gated_commands_for_known_claims(monkeypatch):
    from app import model_router
    claims = [{"id": "S:T12", "kind": "step", "node_key": "T12", "text": "container 111 running"},
              {"id": "F:1", "kind": "fact", "text": "backend listens on 3001"}]
    class _Resp:  # tool_call returns a response read_tool_args can parse
        text = ""
        tool_calls = [{"name": "plan_state_probes", "arguments": {"probes": [
            {"id": "S:T12", "command": "pct start 111", "expect": "running"},
            {"id": "F:1", "command": "ss -tlnp | grep 3001", "expect": "LISTEN"},
            {"id": "X:9", "command": "ls", "expect": ""}]}}]
    monkeypatch.setattr(model_router, "tool_call", AsyncMock(return_value=_Resp()))
    import app.utils.tool_call_args as tca
    monkeypatch.setattr(tca, "read_tool_args", lambda resp: resp.tool_calls[0]["arguments"])
    probes, refused = await sc.plan_probes(claims, {"profile": "root@pve"})
    assert [p["id"] for p in probes] == ["F:1"] and refused == [{"id": "S:T12", "command": "pct start 111"}]
    script = sc.render_probe_script(probes)
    assert script.splitlines() == ['echo "== F:1 =="', "ss -tlnp | grep 3001"]
    assert "every command only reads" in sc.render_probe_message(probes, checked=1, unchecked=1)


async def test_judge_accepts_verdicts_only_for_probed_claims_with_output(monkeypatch):
    from app import model_router
    probes = [{"id": "S:T12", "kind": "step", "node_key": "T12", "claim": "container 111 running", "command": "pct status 111", "expect": "running"},
              {"id": "F:1", "kind": "fact", "node_key": None, "claim": "backend on 3001", "command": "ss -tlnp | grep 3001", "expect": "LISTEN"}]
    pasted = '== S:T12 ==\nstatus: stopped\n'
    class _Resp:
        text = ""
        tool_calls = [{"name": "record_state_verdicts", "arguments": {"verdicts": [
            {"id": "S:T12", "verdict": "contradicted", "reason": "status: stopped", "repair": "Start container 111 (pct start 111)"},
            {"id": "F:1", "verdict": "contradicted", "reason": "invented"},
            {"id": "Z:1", "verdict": "confirmed"}]}}]
    monkeypatch.setattr(model_router, "tool_call", AsyncMock(return_value=_Resp()))
    import app.utils.tool_call_args as tca
    monkeypatch.setattr(tca, "read_tool_args", lambda resp: resp.tool_calls[0]["arguments"])
    verdicts = {v["id"]: v for v in await sc.judge_outputs(probes, pasted)}
    assert verdicts["S:T12"]["verdict"] == "contradicted" and verdicts["S:T12"]["repair"].startswith("Start container")
    assert verdicts["F:1"]["verdict"] == "unknown"  # marker absent → the model's verdict is ignored
    text = sc.render_verdicts(list(verdicts.values()))
    assert "1 contradicted" in text and "❌ `S:T12`" in text and "plan-change proposal" in text


async def test_a_present_but_empty_section_is_judged_not_skipped(monkeypatch):
    """Live (session ac3f0b5d): `docker ps --filter name=^/uptime-kuma$` printed
    nothing — which IS the contradiction — and the claim came back unknown."""
    from app import model_router
    probes = [{"id": "S:T4", "kind": "step", "node_key": "T4", "claim": "container running", "command": "docker ps --filter name=^/x$", "expect": "a row"}]
    seen = {}
    class _Resp:
        text = ""
        tool_calls = [{"name": "record_state_verdicts", "arguments": {"verdicts": [{"id": "S:T4", "verdict": "contradicted", "reason": "no rows", "repair": "Start it"}]}}]
    async def tc(**kw):
        seen["msg"] = kw["messages"][0]["content"]; return _Resp()
    monkeypatch.setattr(model_router, "tool_call", tc)
    import app.utils.tool_call_args as tca
    monkeypatch.setattr(tca, "read_tool_args", lambda resp: resp.tool_calls[0]["arguments"])
    out = {v["id"]: v for v in await sc.judge_outputs(probes, "== S:T4 ==\n")}
    assert out["S:T4"]["verdict"] == "contradicted" and "printed nothing" in seen["msg"]


def test_the_state_check_is_wired_on_every_entry_and_applied_only_on_confirm():
    root = pathlib.Path(__file__).resolve().parents[1]
    turn = (root / "app/modules/assist_turn.py").read_text()
    assert 'if command == "verify_state":' in turn
    assert "STATE_CHECK_PHRASE_RE.search(text_)" in turn and "looks_like_probe_output(text_)" in turn
    fix = turn[turn.index("async def _fix_flow("):turn.index("def _is_must_claim_first(")]
    assert "assist_state_check_after_fixes" in fix and "state_check_done_on_step" in fix and "offer_text(" in fix
    assert turn.index("_fix_failure_streak(") < turn.index('yield _ev(ASSIST_TURN_STATUS, {"text": status_text})', turn.index("async def _fix_flow("))
    router = (root / "app/routers/assist.py").read_text()
    assert '"verify_state"' in router
    replan = (root / "app/modules/assist_replan.py").read_text()
    assert 'p.get("action") == "repair"' in replan and "add_step(session_id=session_id, request=req, db=db)" in replan
    mod = (root / "app/modules/assist_state_check.py").read_text()
    resolve = mod[mod.index("async def resolve_state_check("):mod.index("async def state_check_done_on_step(")]
    assert "_stage_replan_proposal(" in resolve and "add_step(" not in resolve and "UPDATE dag_nodes" not in resolve
    spa = (root / "app/ui/static/views/assist.js").read_text()
    assert 'runTurnStream({ command: "verify_state" })' in spa and "repair: { icon:" in spa


def test_the_phrase_gate_recognises_the_operators_ways_of_asking():
    for t in ("verify state", "Verify the state", "check the build", "state check please", "check where we are", "🩺"):
        assert sc.STATE_CHECK_PHRASE_RE.search(t), t
    for t in ("verify the DNS entry works", "check_mk is installed", "the state of the union"):
        assert not sc.STATE_CHECK_PHRASE_RE.search(t), t


def test_offer_text_names_the_streak_and_the_button():
    t = sc.offer_text(4)
    assert "4 fixes" in t and "Verify state" in t and "verify state" in t


def test_proposals_repair_contradicted_facts_and_steps_and_reopen_steps_without_a_repair():
    verdicts = [
        {"id": "F:3", "kind": "fact", "node_key": None, "verdict": "contradicted", "claim": "container uptime-kuma running", "reason": "no such container", "repair": "Start the uptime-kuma container"},
        {"id": "F:5", "kind": "fact", "node_key": None, "verdict": "contradicted", "claim": "responds on 3001", "reason": "connection refused", "repair": "Start the uptime-kuma container"},
        {"id": "S:T6", "kind": "step", "node_key": "T6", "verdict": "contradicted", "claim": "server block created", "reason": "file missing", "repair": ""},
        {"id": "F:1", "kind": "fact", "node_key": None, "verdict": "contradicted", "claim": "dns ok", "reason": "NXDOMAIN", "repair": ""},
        {"id": "F:2", "kind": "fact", "node_key": None, "verdict": "confirmed", "claim": "certbot", "reason": "", "repair": ""},
    ]
    out = sc.proposals_from_verdicts(verdicts, anchor_node_key="T7")
    assert [(p["action"], p["node_key"]) for p in out] == [("repair", "T7"), ("reopen", "T6")]  # duplicate repair folded, fact-without-repair only retracted


def test_consequences_fold_into_their_root_cause_and_near_duplicate_repairs_collapse():
    """Live (session ac3f0b5d): three contradicted facts about one missing
    container became three repair steps."""
    verdicts = [
        {"id": "F:3", "kind": "fact", "verdict": "contradicted", "claim": "container uptime-kuma running", "reason": "no rows", "repair": "Create and start the Docker container 'uptime-kuma' from image louislam/uptime-kuma:1", "caused_by": ""},
        {"id": "F:4", "kind": "fact", "verdict": "contradicted", "claim": "bound to 127.0.0.1:3001", "reason": "no such object", "repair": "Create the 'uptime-kuma' container with host port 127.0.0.1:3001", "caused_by": "F:3"},
        {"id": "F:5", "kind": "fact", "verdict": "contradicted", "claim": "responds on 3001", "reason": "refused", "repair": "Start Uptime Kuma listening on 127.0.0.1:3001 by running the uptime-kuma container", "caused_by": ""},
    ]
    out = sc.proposals_from_verdicts(verdicts, anchor_node_key="T3")
    assert len(out) == 1 and out[0]["action"] == "repair" and "uptime-kuma" in out[0]["proposed_change"]


async def test_expected_text_present_confirms_without_the_judge_and_batches_are_small(monkeypatch):
    from app import model_router
    probes = [{"id": f"F:{i}", "kind": "fact", "node_key": None, "claim": f"claim {i}", "command": "x", "expect": "certbot 2.9.0"} for i in range(1, 8)]
    pasted = "".join(f"== F:{i} ==\n{'certbot 2.9.0' if i <= 2 else 'something else'}\n" for i in range(1, 8))
    calls = []
    class _Resp:
        text = ""
        tool_calls = [{"name": "record_state_verdicts", "arguments": {"verdicts": []}}]
    async def tc(**kw):
        calls.append(kw["messages"][0]["content"]); return _Resp()
    monkeypatch.setattr(model_router, "tool_call", tc)
    import app.utils.tool_call_args as tca
    monkeypatch.setattr(tca, "read_tool_args", lambda resp: resp.tool_calls[0]["arguments"])
    out = {v["id"]: v for v in await sc.judge_outputs(probes, pasted)}
    assert out["F:1"]["verdict"] == "confirmed" and out["F:2"]["verdict"] == "confirmed"
    assert out["F:3"]["verdict"] == "unknown" and out["F:3"]["reason"] == "the judge returned no verdict for this check"
    assert len(calls) == 1 and "EACH of the 5 claims" in calls[0]  # 5 unjudged → one batch of 5


async def test_probes_are_requested_in_batches_pins_are_context_and_progress_is_reported(monkeypatch):
    """Live (operator session, 86 claims): one call over all claims returned no
    probes and the operator saw an empty check."""
    from app import model_router
    claims = [{"id": f"S:T{i}", "kind": "step", "node_key": f"T{i}", "text": f"step {i} done"} for i in range(1, 24)]
    claims += [{"id": "K:HOST", "kind": "pin", "text": "HOST = pve"}]
    calls = []
    class _Resp:
        def __init__(self, ids): self.tool_calls = [{"name": "plan_state_probes", "arguments": {"probes": [{"id": i, "command": "pct list", "expect": "running"} for i in ids]}}]; self.text = ""
    async def tc(**kw):
        msg = kw["messages"][0]["content"]; calls.append(msg)
        ids = [ln.split(":")[0].strip("- ") + ":" + ln.split(":")[1].split()[0] for ln in msg.split("CLAIMS")[1].splitlines() if ln.startswith("- S:")]
        return _Resp(ids)
    monkeypatch.setattr(model_router, "tool_call", tc)
    import app.utils.tool_call_args as tca
    monkeypatch.setattr(tca, "read_tool_args", lambda resp: resp.tool_calls[0]["arguments"])
    progress = []
    async def prog(i, n, so_far): progress.append((i, n))
    probes, refused = await sc.plan_probes(claims, {"profile": "root@pve"}, on_progress=prog)
    assert len(calls) == 3 and progress == [(1, 3), (2, 3), (3, 3)]  # 23 step claims → 10+10+3; the pin is context
    assert len(probes) == 23 and all("KNOWN VALUES" in c and "HOST = pve" in c for c in calls)
    assert "K:HOST" not in {p["id"] for p in probes}


def test_an_empty_probe_result_is_explained_as_a_miss_not_as_nothing_to_check():
    assert "returned no usable command for any of the 86 claims" in sc.render_probe_message([], checked=0, unchecked=86)
    assert "recorded nothing I can verify" in sc.render_probe_message([], checked=0, unchecked=0)


# ---- §17.1052 — safety: history is not a claim, repairs never destroy ---------

@pytest.mark.parametrize("fact", [
    "VM 100 AI-VM destroyed with purge; logical volumes vm-100-disk-0 removed",
    "The /etc/caddy/Caddyfile in LXC 120 was truncated with 'truncate -s 0'",
    "The operator's attempt to start the backend used the path /opt/control-panel (with a typo)",
    "'systemctl start control-panel.service' fails with 'Unit not found'",
    "Backup of /etc/network/interfaces created (interfaces.bak)",
])
def test_history_and_failure_facts_are_not_probed(fact):
    assert not sc.probe_worthy(fact), fact


@pytest.mark.parametrize("fact", [
    "VM 106 (palworld-server) is running with 4 cores and 8192 MB",
    "Inside LXC 111 the control-panel backend listens on *:3001 as the transient unit control-panel",
    "cluster.fw has policy_in DROP with five security groups",
])
def test_desired_state_facts_are_probed(fact):
    assert sc.probe_worthy(fact), fact


@pytest.mark.parametrize("repair", [
    "Stop the node process (pid 430) listening on *:3001 inside LXC 111",
    "Stop LXC container 120 and truncate its /etc/caddy/Caddyfile to 0 bytes",
    "Remove every remaining VM (106, 110) and container from the Proxmox host",
    "rm -rf /opt/control-panel and reinstall",
])
def test_destructive_repairs_are_refused(repair):
    assert sc.destructive_repair(repair), repair


def test_a_destructive_repair_from_the_judge_becomes_needs_decision_and_never_a_proposal():
    verdicts = [{"id": "F:1", "kind": "fact", "node_key": None, "verdict": "contradicted", "claim": "backend listening on 3001",
                 "reason": "something IS listening", "repair": "Stop the node process listening on 3001"},
                {"id": "S:T6", "kind": "step", "node_key": "T6", "verdict": "contradicted", "claim": "firewall groups carry -source",
                 "reason": "no -source", "repair": "Add -source 192.168.1.0/24 to the game and dmz group rules"}]
    out = sc.proposals_from_verdicts(verdicts, anchor_node_key="T37")
    assert [(p["action"], p["node_key"]) for p in out] == [("repair", "T6")]
    text = sc.render_verdicts([dict(verdicts[0], needs_decision=True, repair="")])
    assert "I am not proposing one; you decide" in text


async def test_judge_strips_destructive_repairs_at_the_source(monkeypatch):
    from app import model_router
    probes = [{"id": "F:1", "kind": "fact", "node_key": None, "claim": "backend on 3001", "command": "ss -tlnp", "expect": "nothing"}]
    class _Resp:
        text = ""
        tool_calls = [{"name": "record_state_verdicts", "arguments": {"verdicts": [
            {"id": "F:1", "verdict": "contradicted", "reason": "LISTEN", "repair": "Kill the process on 3001"}]}}]
    monkeypatch.setattr(model_router, "tool_call", AsyncMock(return_value=_Resp()))
    import app.utils.tool_call_args as tca
    monkeypatch.setattr(tca, "read_tool_args", lambda resp: resp.tool_calls[0]["arguments"])
    v = (await sc.judge_outputs(probes, "== F:1 ==\nLISTEN 0 511 *:3001\n"))[0]
    assert v["verdict"] == "contradicted" and v["repair"] == "" and v.get("needs_decision") is True


def test_the_judge_prompt_forbids_destructive_repairs():
    assert "never propose" in sc._JUDGE_OPENING and "destructive" in sc._JUDGE_OPENING
