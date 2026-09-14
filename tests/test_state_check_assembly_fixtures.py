"""§17.1071 — fixture-driven tests over the state-check ASSEMBLY.

The mutation baseline (§17.1062) left ~450 survivors in build_claims,
plan_probes, judge_outputs, render_* and proposals_from_verdicts: every
existing test drove them with one or two synthetic items, so a mutant that
changed a batch boundary, dropped a field, mis-keyed a dict or reversed an
order still passed. These fixtures are the SHAPES the live homelab check
produced (2026-09-13: 90 claims, 48 probes, 29/12/7 verdicts), reduced,
and each assertion pins a field or an ordering a mutant would move.
"""
from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import model_router
from app.modules import assist_state_check as sc

pytestmark = pytest.mark.asyncio

# ── build_claims: recap DONE lines win over evidence, ids/kinds/order, facts filter, pins ──

_STEP_ROWS = [
    {"node_key": "T4", "title": "Configure ZFS mirror pool", "evidence": "root@pve:~# zpool status\n  pool: oasis ONLINE", "progress_recap": "DONE: Created the oasis mirror on sda/sdb\nDONE: Set compression=lz4\nDONE: Mounted at /oasis\nDONE: extra line that must be dropped", "committed_at": None},
    {"node_key": "T6", "title": "Write the firewall policy", "evidence": "cluster.fw written with policy_in DROP and five security groups", "progress_recap": None, "committed_at": None},
    {"node_key": "T9", "title": "Download the Debian template", "evidence": "", "progress_recap": "", "committed_at": None},
    {"node_key": "ADD10", "title": "Stop the control-panel backend node process on port 3001", "evidence": "systemctl stop control-panel", "progress_recap": "DONE: Ran systemctl stop", "committed_at": None},
]
_ENV = {"facts": ["LXC container 105 (download-client) exists and is running",
                  "Worked at T37: pct status 120 → status: running",
                  "The attempt to write the Caddyfile failed with EOF",   # history → dropped
                  "cluster.fw security group 'game' has IN ACCEPT udp dport 8211"],
        "substitutions": {"DOMAIN": "defrusciohomelab.duckdns.org", "PVE_IP": "192.168.1.156"}}


def _res_first(row):
    r = MagicMock(); r.mappings.return_value.first.return_value = row; return r


def _res_all(rows):
    r = MagicMock(); r.mappings.return_value.all.return_value = rows; return r


async def test_build_claims_shapes_ids_kinds_and_order_from_live_rows():
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _res_first({"job_id": "j1", "metadata": {"environment": _ENV}, "current_node_key": "ADD13"}),
        _res_all(_STEP_ROWS),
    ])
    with patch("app.modules.assist_environment._environment_from_metadata", return_value=_ENV), \
         patch("app.modules.assist_render.parse_recap",
               side_effect=lambda r: {"done": [l[6:] for l in (r or "").splitlines() if l.startswith("DONE: ")]}):
        out = await sc.build_claims(db=db, session_id="s1", max_facts=24)
    claims = out["claims"]
    ids = [c["id"] for c in claims]
    # order: steps (in execution order) → facts (F:1..) → pins (K:…); the history step ADD10 is skipped
    assert ids == ["S:T4", "S:T6", "S:T9", "F:1", "F:2", "F:3", "K:DOMAIN", "K:PVE_IP"]
    by = {c["id"]: c for c in claims}
    assert by["S:T4"]["text"] == "Created the oasis mirror on sda/sdb; Set compression=lz4; Mounted at /oasis"  # first THREE done lines, "; "-joined
    assert by["S:T6"]["text"].startswith("cluster.fw written") and by["S:T6"]["kind"] == "step" and by["S:T6"]["node_key"] == "T6"
    assert by["S:T9"]["text"] == "step completed: Download the Debian template"   # no recap, no evidence → title fallback
    assert by["F:1"]["kind"] == "fact" and "node_key" not in by["F:1"]
    assert by["F:3"]["text"].startswith("cluster.fw security group")               # the history fact was filtered, numbering is dense
    assert by["K:DOMAIN"] == {"id": "K:DOMAIN", "kind": "pin", "text": "DOMAIN = defrusciohomelab.duckdns.org"}
    assert out["job_id"] == "j1" and out["current_node_key"] == "ADD13" and out["environment"] is _ENV


async def test_build_claims_respects_max_facts_keeping_the_newest():
    env = {"facts": [f"fact number {i} is true" for i in range(30)], "substitutions": {}}
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_res_first({"job_id": "j1", "metadata": {}, "current_node_key": None}), _res_all([])])
    with patch("app.modules.assist_environment._environment_from_metadata", return_value=env), \
         patch("app.modules.assist_render.parse_recap", return_value={}):
        out = await sc.build_claims(db=db, session_id="s1", max_facts=5)
    texts = [c["text"] for c in out["claims"]]
    assert texts == [f"fact number {i} is true" for i in range(25, 30)]
    assert [c["id"] for c in out["claims"]] == ["F:1", "F:2", "F:3", "F:4", "F:5"]


# ── plan_probes: batching, dedupe, gate, caps, field carry-through ──

def _claims(n: int, kind="step"):
    return [{"id": f"S:T{i}", "kind": kind, "node_key": f"T{i}", "title": f"Step {i}", "text": f"claim {i}"} for i in range(1, n + 1)]


def _probe_resp(items):
    r = MagicMock(); r.tool_calls = [{"name": "plan_state_probes", "arguments": {"probes": items}}]; return r


async def test_plan_probes_batches_by_ten_dedupes_and_carries_claim_fields(monkeypatch):
    claims = _claims(23) + [{"id": "K:X", "kind": "pin", "text": "X = 1"}]
    calls = []

    async def fake(messages, tools, **kw):
        body = messages[0]["content"]
        ids = re.findall(r"^- (S:T\d+):", body.split("CLAIMS")[1], re.M)
        calls.append(ids)
        items = [{"id": i, "command": f"cat /etc/{i.lower()}", "expect": f"ok {i}"} for i in ids]
        items.append({"id": ids[0], "command": "echo dup"})          # duplicate id → ignored
        items.append({"id": "S:T999", "command": "cat /x"})          # unknown id → ignored
        items.append({"id": ids[1], "command": ""})                  # empty command → ignored
        return _probe_resp(items)

    monkeypatch.setattr(model_router, "tool_call", fake)
    import app.utils.tool_call_args as tca
    monkeypatch.setattr(tca, "read_tool_args", lambda resp: resp.tool_calls[0]["arguments"])
    progress = []
    probes, refused = await sc.plan_probes(claims, {"profile": "root@pve"}, on_progress=lambda bi, n, got: progress.append((bi, n, got)))
    assert [len(c) for c in calls] == [10, 10, 3]                    # 23 targets, pins excluded, batches of 10
    assert progress == [(1, 3, 0), (2, 3, 10), (3, 3, 20)]
    assert len(probes) == 23 and refused == []
    assert probes[0] == {"id": "S:T1", "command": "cat /etc/s:t1", "expect": "ok S:T1", "claim": "claim 1", "kind": "step", "node_key": "T1"}
    assert [p["id"] for p in probes] == [f"S:T{i}" for i in range(1, 24)]  # order preserved across batches


async def test_plan_probes_refuses_mutations_and_stops_at_the_cap(monkeypatch):
    claims = _claims(60)

    async def fake(messages, tools, **kw):
        body = messages[0]["content"]
        ids = re.findall(r"^- (S:T\d+):", body.split("CLAIMS")[1], re.M)
        items = []
        for i in ids:
            n = int(i[3:])
            items.append({"id": i, "command": "systemctl restart caddy" if n % 7 == 0 else f"systemctl is-active svc{n}"})
        return _probe_resp(items)

    monkeypatch.setattr(model_router, "tool_call", fake)
    import app.utils.tool_call_args as tca
    monkeypatch.setattr(tca, "read_tool_args", lambda resp: resp.tool_calls[0]["arguments"])
    probes, refused = await sc.plan_probes(claims, None)
    assert len(probes) == sc._MAX_PROBES
    assert all("restart" not in p["command"] for p in probes)
    assert refused and all(r["command"] == "systemctl restart caddy" for r in refused)
    assert probes[-1]["id"] == f"S:T{max(int(p['id'][3:]) for p in probes)}"


async def test_plan_probes_model_failure_in_one_batch_keeps_the_others(monkeypatch):
    claims = _claims(20)
    n = {"calls": 0}

    async def fake(messages, tools, **kw):
        n["calls"] += 1
        if n["calls"] == 1:
            raise RuntimeError("model down")
        body = messages[0]["content"]
        ids = re.findall(r"^- (S:T\d+):", body.split("CLAIMS")[1], re.M)
        return _probe_resp([{"id": i, "command": f"cat /etc/{i.lower()}"} for i in ids])

    monkeypatch.setattr(model_router, "tool_call", fake)
    import app.utils.tool_call_args as tca
    monkeypatch.setattr(tca, "read_tool_args", lambda resp: resp.tool_calls[0]["arguments"])
    probes, _ = await sc.plan_probes(claims, None)
    assert [p["id"] for p in probes] == [f"S:T{i}" for i in range(11, 21)]


# ── judge_outputs: sections, expected-text pre-pass, batches of five, field carry-through ──

_PROBES = [
    {"id": "S:T1", "command": "pct status 101", "expect": "status: running", "claim": "LXC 101 running", "kind": "step", "node_key": "T1", "title": "Create Jellyfin LXC"},
    {"id": "S:T2", "command": "qm status 106", "expect": "", "claim": "VM 106 running", "kind": "step", "node_key": "T2", "title": "Start VM 106"},
    {"id": "F:1", "command": "ss -tlnp | grep 3001", "expect": "3001", "claim": "backend listens on 3001", "kind": "fact", "node_key": None, "title": ""},
    {"id": "F:2", "command": "wc -c /etc/caddy/Caddyfile", "expect": "", "claim": "Caddyfile has 15 lines", "kind": "fact", "node_key": None, "title": ""},
    {"id": "F:3", "command": "tailscale status", "expect": "", "claim": "tailscale up", "kind": "fact", "node_key": None, "title": ""},
    {"id": "F:4", "command": "cat /proc/driver/nvidia/version", "expect": "", "claim": "driver 580", "kind": "fact", "node_key": None, "title": ""},
]
_PASTE = """root@pve:~# echo "== S:T1 =="; pct status 101
== S:T1 ==
status: running
root@pve:~# echo "== S:T2 =="; qm status 106
== S:T2 ==
status: stopped
root@pve:~# echo "== F:1 =="; ss -tlnp | grep 3001
== F:1 ==
root@pve:~# echo "== F:2 =="; wc -c /etc/caddy/Caddyfile
== F:2 ==
406 /etc/caddy/Caddyfile
root@pve:~# echo "== F:3 =="; tailscale status
== F:3 ==
100.84.253.90 pve linux -
"""


async def test_judge_prepass_batches_and_verdict_fields(monkeypatch):
    seen_batches = []

    async def fake(messages, tools, **kw):
        body = messages[0]["content"]
        ids = [l[1:l.index("]")] for l in body.splitlines() if l.startswith("[") and "] CLAIM:" in l]
        seen_batches.append(ids)
        out = []
        for i in ids:
            if i == "S:T2":
                out.append({"id": i, "verdict": "contradicted", "reason": "status: stopped", "repair": "Start VM 106 with qm start 106", "caused_by": ""})
            elif i == "F:1":
                out.append({"id": i, "verdict": "contradicted", "reason": "no listener", "repair": "Stop and remove the old unit", "caused_by": "S:T2"})
            elif i == "F:2":
                out.append({"id": i, "verdict": "unknown", "reason": "bytes not lines"})
            elif i == "F:3":
                pass                                                 # only the bad verdict below
            else:
                out.append({"id": i, "verdict": "confirmed", "reason": "ok"})
        out.append({"id": "F:9", "verdict": "confirmed"})           # not probed → ignored
        out.append({"id": "F:3", "verdict": "maybe"})               # bad verdict → ignored
        r = MagicMock(); r.tool_calls = [{"name": "record_verdicts", "arguments": {"verdicts": out}}]; return r

    monkeypatch.setattr(model_router, "tool_call", fake)
    import app.utils.tool_call_args as tca
    monkeypatch.setattr(tca, "read_tool_args", lambda resp: resp.tool_calls[0]["arguments"])
    verdicts = await sc.judge_outputs(_PROBES, _PASTE)
    by = {v["id"]: v for v in verdicts}
    assert set(by) == {p["id"] for p in _PROBES}
    assert by["S:T1"]["verdict"] == "confirmed" and "expected 'status: running'" in by["S:T1"]["reason"]  # pre-pass, no model
    assert "S:T1" not in sum(seen_batches, [])
    assert by["S:T2"] == {"id": "S:T2", "verdict": "contradicted", "reason": "status: stopped", "repair": "Start VM 106 with qm start 106",
                          "caused_by": "", "kind": "step", "node_key": "T2", "claim": "VM 106 running", "title": "Start VM 106"}
    assert by["F:1"]["repair"] == "" and by["F:1"]["needs_decision"] is True and by["F:1"]["caused_by"] == "S:T2"  # destructive repair stripped
    assert by["F:2"]["verdict"] == "unknown" and by["F:2"]["reason"] == "bytes not lines"
    assert by["F:3"]["verdict"] == "unknown" and by["F:3"]["reason"] == "the judge returned no verdict for this check"  # bad verdict ignored
    assert by["F:4"]["verdict"] == "unknown" and by["F:4"]["reason"] == "no output pasted for this check"          # no section at all
    assert all(len(b) <= sc._JUDGE_BATCH for b in seen_batches) and sum(len(b) for b in seen_batches) == 4


async def test_judge_with_no_sections_returns_all_unknown_without_calling_the_model(monkeypatch):
    tc = AsyncMock()
    monkeypatch.setattr(model_router, "tool_call", tc)
    verdicts = await sc.judge_outputs(_PROBES, "root@pve:~# ls\nnothing marked\n")
    assert len(verdicts) == len(_PROBES) and all(v["verdict"] == "unknown" for v in verdicts)
    tc.assert_not_awaited()


# ── render_probe_message / render_verdicts: counts, ordering, wording ──

def test_render_probe_message_lists_at_most_twenty_and_names_the_unchecked():
    probes = [{"id": f"S:T{i}", "command": f"cat /etc/{i}", "claim": f"claim {i}", "kind": "step"} for i in range(1, 26)]
    msg = sc.render_probe_message(probes, checked=25, unchecked=3)
    assert "25 things the plan believes" in msg
    assert msg.count("- `S:T") == 20
    assert "(3 claims cannot be checked from a shell and are left as they are.)" in msg
    assert "```bash" in msg and "== S:T25 ==" in msg
    one = sc.render_probe_message(probes[:1], checked=1, unchecked=1)
    assert "1 thing the plan believes" in one and "(1 claim cannot be checked" in one
    assert "could not build the checks" in sc.render_probe_message([], checked=0, unchecked=4)
    assert "recorded nothing I can verify" in sc.render_probe_message([], checked=0, unchecked=0)


def test_render_verdicts_orders_contradicted_then_unknown_then_confirmed_with_counts():
    vs = [
        {"id": "F:2", "verdict": "confirmed", "claim": "c2", "reason": "ok"},
        {"id": "S:T1", "verdict": "unknown", "claim": "u1", "reason": "no output"},
        {"id": "S:T3", "verdict": "contradicted", "claim": "x3", "reason": "stopped", "needs_decision": True},
        {"id": "F:1", "verdict": "contradicted", "claim": "x1", "reason": "missing"},
    ]
    text = sc.render_verdicts(vs)
    assert "2 confirmed" not in text and "1 confirmed, 2 contradicted, 1 unknown" in text
    order = [l.split("`")[1] for l in text.splitlines() if l.startswith("- ")]
    assert order == ["S:T3", "F:1", "S:T1", "F:2"]
    assert "— stopped" in text and "you decide" in text and "— ok" not in text   # confirmed lines carry no reason
    assert "retracted from what I believe" in text
    assert "Everything I could check still holds" in sc.render_verdicts([vs[0]])


# ── proposals_from_verdicts: fold, refuse, anchor, dedupe ──

def test_proposals_fold_consequences_dedupe_by_terms_and_anchor_facts():
    vs = [
        {"id": "S:T2", "kind": "step", "node_key": "T2", "title": "Start VM 106", "verdict": "contradicted", "claim": "VM 106 running", "reason": "stopped", "repair": "Start VM 106 (palworld-server) with qm start 106"},
        {"id": "F:1", "kind": "fact", "node_key": None, "title": "", "verdict": "contradicted", "claim": "palworld reachable", "reason": "refused", "repair": "Start the palworld VM (qm start 106) so the game port answers", "caused_by": "S:T2"},
        {"id": "F:5", "kind": "fact", "node_key": None, "title": "", "verdict": "contradicted", "claim": "port 8211 open", "reason": "closed", "repair": "qm start 106 palworld-server then re-check 8211"},
        {"id": "S:T6", "kind": "step", "node_key": "T6", "title": "Write the firewall policy", "verdict": "contradicted", "claim": "cluster.fw has five groups", "reason": "file missing", "repair": ""},
        {"id": "F:7", "kind": "fact", "node_key": None, "title": "", "verdict": "contradicted", "claim": "dns ok", "reason": "NXDOMAIN", "repair": ""},
        {"id": "F:8", "kind": "fact", "node_key": None, "title": "", "verdict": "confirmed", "claim": "x", "reason": "", "repair": "Start something"},
    ]
    out = sc.proposals_from_verdicts(vs, anchor_node_key="ADD13")
    assert [(p["action"], p["node_key"]) for p in out] == [("repair", "T2"), ("reopen", "T6")]
    assert out[0]["proposed_change"] == "Start VM 106 (palworld-server) with qm start 106" and out[0]["reason"] == "stopped"
    assert out[0]["current_assumption"] == "VM 106 running"
    assert out[1]["proposed_change"].startswith("Redo this step — file missing")
    # F:1 folded into S:T2 (caused_by); F:5 deduped against T2's repair by shared identifiers (palworld-server);
    # F:7 has no repair → retracted only; F:8 not contradicted. NB: a repair with no identifier-ish token
    # ("Start VM 106 with qm start 106") has NO terms and can never fold — worth knowing when reading live output.
    assert not any(p.get("node_key") == "ADD13" for p in out)


def test_proposals_without_an_anchor_skip_fact_repairs_and_keep_step_ones():
    vs = [{"id": "F:1", "kind": "fact", "node_key": None, "title": "", "verdict": "contradicted", "claim": "c", "reason": "r", "repair": "Start the thing"},
          {"id": "S:T1", "kind": "step", "node_key": "T1", "title": "Build it", "verdict": "contradicted", "claim": "built", "reason": "missing", "repair": "Rebuild the thing on host"}]
    out = sc.proposals_from_verdicts(vs, anchor_node_key=None)
    assert [(p["action"], p["node_key"]) for p in out] == [("repair", "T1")]
