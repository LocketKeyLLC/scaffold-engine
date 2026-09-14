"""§17.1054 — a state check must not reopen one-off steps, and the transcript
must not show every paste twice.

Live (session 613dd1df, 2026-09-13 23:28): the operator's second 🩺 press
judged five PAST ACTIONS as contradicted — "VM 100 destroyed" (T3), "Ran
systemctl stop" (ADD10), "Caddyfile truncated to 0 bytes" (ADD11), "Collected
raw state into a file" (T1), "VMID 102 confirmed free" (T13) — and one clipped
claim ("tags: me", from "tags: media"). Apply reopened six finished steps and
inserted four repairs chained in reverse; the pointer sat on the last repair
while the dep gate served the reopened T1, so the operator's paste for one
step was judged against another. Separately every paste rendered twice
(message row + submit row) and every answer twice (clock-based dedupe).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import assist_state_check as sc
from app.modules import assist_turns
from app.modules import assist_replan


# ── history step claims are not probed ───────────────────────────────────────

@pytest.mark.parametrize("claim", [
    "VM 100 AI-VM destroyed with purge; logical volumes vm-100-disk-0 removed",
    "Ran `systemctl stop control-panel.service` in LXC 111 (no error).; is-active → inactive",
    "Caddyfile truncated to 0 bytes (confirmed via wc -c returning 0); Container 120 stopped",
    "Collected raw state into /root/proxmox-current-state.txt; Created summary file",
    "VMID 102 confirmed free.; IP 192.168.1.21 confirmed free.; `pct create 102` executed",
])
def test_one_off_step_results_are_history(claim):
    assert not sc.probe_worthy(claim), claim


@pytest.mark.parametrize("claim", [
    "cluster.fw written with policy_in DROP and five security groups",
    "VM 110 created and configured with GPU passthrough (Tesla P40); Ubuntu installed",
    "Added bind mount mp0 /oasis/media -> /media (read-only) to LXC 101",
])
def test_desired_state_step_results_are_probed(claim):
    assert sc.probe_worthy(claim), claim


def _result_all(rows):
    r = MagicMock(); r.mappings.return_value.all.return_value = rows; return r


def _result(row):
    r = MagicMock(); r.mappings.return_value.first.return_value = row; return r


@pytest.mark.asyncio
async def test_build_claims_skips_history_steps_and_destructive_titles_and_clips_on_a_word():
    long_evidence = "root@pve:~# pct create 104 local:vztmpl/debian-12 " + "x" * 230 + " -tags media -unprivileged 1"
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _result({"job_id": "j1", "metadata": {}, "current_node_key": "ADD13"}),
        _result_all([
            {"node_key": "T1", "title": "Audit existing Proxmox environment", "evidence": "", "progress_recap": "DONE: Collected raw state into /root/state.txt", "committed_at": None},
            {"node_key": "T3", "title": "Remove old containers and VMs", "evidence": "", "progress_recap": "DONE: cleaned up", "committed_at": None},
            {"node_key": "ADD10", "title": "Stop the control-panel backend node process on port 3001", "evidence": "", "progress_recap": "DONE: done", "committed_at": None},
            {"node_key": "T6", "title": "Write the firewall policy", "evidence": "", "progress_recap": "DONE: cluster.fw written with policy_in DROP", "committed_at": None},
            {"node_key": "T17", "title": "Create the qBittorrent LXC", "evidence": long_evidence, "progress_recap": None, "committed_at": None},
        ]),
    ])
    with patch("app.modules.assist_environment._environment_from_metadata", return_value={"facts": [], "substitutions": {}}), \
         patch("app.modules.assist_render.parse_recap", side_effect=lambda r: {"done": [r.split("DONE: ", 1)[1]]} if r else {}):
        out = await sc.build_claims(db=db, session_id="s1")
    ids = [c["id"] for c in out["claims"]]
    assert ids == ["S:T6", "S:T17"], ids
    t17 = next(c for c in out["claims"] if c["id"] == "S:T17")
    assert t17["text"].endswith(" …") and len(t17["text"]) <= 242  # evidence head: 240 on a word + ellipsis
    assert not t17["text"].endswith("tags: me …")  # never a mid-word clip


def test_clip_claim_marks_and_cuts_on_a_word_boundary():
    t = ("word " * 80).strip()
    out = sc._clip_claim(t, limit=100)
    assert out.endswith(" …") and len(out) <= 102 and not out[:-2].endswith("wor")
    assert sc._clip_claim("short") == "short"


# ── reopen gate ──────────────────────────────────────────────────────────────

def test_reopen_of_a_one_off_or_destructive_step_is_refused_and_flagged():
    verdicts = [
        {"id": "S:T3", "kind": "step", "node_key": "T3", "title": "Remove old containers and VMs", "verdict": "contradicted",
         "claim": "VM 100 destroyed with purge", "reason": "qm list shows 106 and 110", "repair": ""},
        {"id": "S:ADD10", "kind": "step", "node_key": "ADD10", "title": "Stop the control-panel backend", "verdict": "contradicted",
         "claim": "Ran systemctl stop control-panel", "reason": "active", "repair": ""},
        {"id": "S:T1", "kind": "step", "node_key": "T1", "title": "Audit existing Proxmox environment", "verdict": "contradicted",
         "claim": "Collected raw state into /root/state.txt", "reason": "pct list differs", "repair": ""},
        {"id": "S:T6", "kind": "step", "node_key": "T6", "title": "Write the firewall policy", "verdict": "contradicted",
         "claim": "cluster.fw has five security groups", "reason": "file missing", "repair": ""},
    ]
    out = sc.proposals_from_verdicts(verdicts, anchor_node_key="ADD13")
    assert [(p["action"], p["node_key"]) for p in out] == [("reopen", "T6")]
    assert all(v.get("needs_decision") == "reopen" for v in verdicts[:3])
    assert verdicts[3].get("needs_decision") is None
    text = sc.render_verdicts(verdicts)
    assert text.count("I am not reopening it; you decide") == 3


def test_judge_rule_and_verdict_shape_carry_the_title():
    assert "Such a claim is contradicted ONLY if the specific object it names is back" in sc._JUDGE_OPENING
    assert "clipped" in sc._JUDGE_OPENING
    src = open("app/modules/assist_state_check.py", encoding="utf-8").read()
    judge = src[src.index("async def judge_outputs("):src.index("def render_verdicts(")]
    assert '"title": p.get("title")' in judge  # reopen_refused reads it


# ── apply: repairs in proposal order, one pointer ────────────────────────────

class _Res:
    def __init__(self, scalar=None, rows=None):
        self._scalar = scalar; self._rows = rows or []
    def scalar(self): return self._scalar
    def mappings(self):
        m = MagicMock(); m.all.return_value = self._rows; m.first.return_value = (self._rows or [None])[0]; return m


@pytest.mark.asyncio
async def test_repairs_anchor_on_the_original_step_in_order_and_pointer_lands_on_the_first():
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_Res(scalar="ADD13"))
    db.commit = AsyncMock()
    calls = []
    async def fake_add_step(*, session_id, request, before_node_key=None, db=None, **_):
        calls.append((request, before_node_key))
        return {"node_key": f"ADD{20 + len(calls)}"}
    proposals = [{"action": "repair", "node_key": "ADD13", "proposed_change": "Complete Jellyfin setup"},
                 {"action": "repair", "node_key": "ADD13", "proposed_change": "Start qBittorrent"}]
    with patch("app.modules.assist_notes.add_step", new=fake_add_step):
        out = await assist_replan.apply_note_replan(db=db, session_id="s1", job_id="j1", proposals=proposals)
    assert out["repaired"] == ["ADD21", "ADD22"]
    assert [b for _, b in calls] == ["ADD13", "ADD13"]          # same anchor every time → chained in order
    sqls = [str(c.args[0]) for c in db.execute.await_args_list]
    pointer = [c for c in db.execute.await_args_list if "current_node_key = :nk" in str(c.args[0])]
    assert pointer and pointer[-1].args[1]["nk"] == "ADD21"      # the FIRST repair, not the last


@pytest.mark.asyncio
async def test_reopen_without_repair_clears_the_pointer_so_the_dep_gate_decides():
    db = AsyncMock()
    # reopen path: SELECT prior → rows, UPDATE dag_nodes RETURNING → rows, then UPDATE steps …
    db.execute = AsyncMock(side_effect=lambda *a, **k: _Res(scalar=None, rows=[{"node_key": "T6", "output_text": "x"}]))
    db.commit = AsyncMock()
    out = await assist_replan.apply_note_replan(db=db, session_id="s1", job_id="j1",
                                                proposals=[{"action": "reopen", "node_key": "T6", "proposed_change": "redo"}])
    assert out["reopened"] == ["T6"]
    assert any("current_node_key = NULL" in str(c.args[0]) for c in db.execute.await_args_list)


# ── transcript: one bubble per paste ─────────────────────────────────────────

def test_collapse_double_records_merges_message_then_submit():
    rows = [
        {"id": 1, "role": "assistant", "kind": "guide", "content": "do X", "evidence_kind": None},
        {"id": 2, "role": "operator", "kind": "message", "content": "root@pve:~# X\nok", "evidence_kind": None},
        {"id": 3, "role": "operator", "kind": "submit", "content": "root@pve:~# X\nok ", "evidence_kind": "text"},
        {"id": 4, "role": "assistant", "kind": "ask", "content": "verified", "evidence_kind": None},
        {"id": 5, "role": "operator", "kind": "message", "content": "next", "evidence_kind": None},
        {"id": 6, "role": "operator", "kind": "message", "content": "next", "evidence_kind": None},  # not a submit → kept
    ]
    out = assist_turns.collapse_double_records(rows)
    assert [r["id"] for r in out] == [1, 2, 4, 5, 6]
    assert out[1]["kind"] == "submit" and out[1]["evidence_kind"] == "text"
    assert assist_turns.collapse_double_records([]) == []
