"""§17.1056 — the five items left open after the 2026-09-13/14 session:
reopen pre-image + restore, decide with thinking off, the indirect-evidence
gate, the remote-host skip for the tool-availability footer, and the confirm
nudge folded into the fix."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import model_router
from app.modules import assist_guide, assist_notes, assist_state_check as sc, assist_turn


# ── restore from the pre-image ──────────────────────────────────────────────

class _Res:
    def __init__(self, scalar=None, first=None):
        self._scalar = scalar; self._first = first
    def scalar(self): return self._scalar
    def mappings(self):
        m = MagicMock(); m.first.return_value = self._first; return m


PRE = {"node_key": "T3", "step_status": "committed", "evidence": "VM 100 destroyed with purge",
       "evidence_kind": "text", "committed_at": "2026-08-27T22:00:00+00:00",
       "output_text": "qm destroy 100 --purge", "completed_at": "2026-08-27T22:00:00+00:00",
       "ts": "2026-09-13T23:28:05Z"}


@pytest.mark.asyncio
async def test_restore_puts_the_step_back_with_its_evidence_and_drops_the_preimage():
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _Res(first={"job_id": "j1", "current_node_key": "T3", "metadata": {"reopen_preimages": [PRE]}}),
        _Res(scalar="presented"),   # step status
        _Res(scalar=0),             # operator turns since the reopen
        _Res(), _Res(), _Res(),     # dag_nodes, assist_steps, session
    ])
    db.commit = AsyncMock()
    out = await assist_notes.restore_reopened_step(session_id="s1", node_key="T3", db=db)
    assert out["restored"] and out["node_key"] == "T3" and out["evidence_chars"] == len(PRE["evidence"])
    sqls = [str(c.args[0]) for c in db.execute.await_args_list]
    params = [c.args[1] for c in db.execute.await_args_list]
    assert any("status = 'done'" in s for s in sqls) and any("status = 'committed'" in s for s in sqls)
    assert params[3]["out"] == PRE["output_text"] and params[4]["ev"] == PRE["evidence"]
    assert params[5]["pre"] == "[]"                       # consumed
    assert "current_node_key = CASE WHEN current_node_key = :nk THEN NULL" in sqls[5]
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_restore_refuses_when_the_operator_worked_on_the_step_since():
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _Res(first={"job_id": "j1", "current_node_key": "T3", "metadata": {"reopen_preimages": [PRE]}}),
        _Res(scalar="presented"),
        _Res(scalar=2),
    ])
    with pytest.raises(ValueError, match="operator turn"):
        await assist_notes.restore_reopened_step(session_id="s1", node_key="T3", db=db)
    assert not any("status = 'done'" in str(c.args[0]) for c in db.execute.await_args_list)


@pytest.mark.asyncio
async def test_restore_refuses_without_a_preimage_or_on_a_finished_step():
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_Res(first={"job_id": "j1", "current_node_key": None, "metadata": {}})])
    with pytest.raises(ValueError, match="no reopen pre-image"):
        await assist_notes.restore_reopened_step(session_id="s1", node_key="T3", db=db)
    db.execute = AsyncMock(side_effect=[
        _Res(first={"job_id": "j1", "current_node_key": None, "metadata": {"reopen_preimages": [PRE]}}),
        _Res(scalar="committed"),
    ])
    with pytest.raises(ValueError, match="only a reopened"):
        await assist_notes.restore_reopened_step(session_id="s1", node_key="T3", db=db)


def test_restore_route_is_declared():
    src = open("app/routers/assist.py", encoding="utf-8").read()
    assert '@router.post("/assist/{session_id}/step/restore")' in src


# ── decide: thinking off from the first draw ────────────────────────────────

@pytest.mark.asyncio
async def test_tool_call_forwards_think_to_every_draw():
    good = MagicMock(); good.success = True
    call = MagicMock(); call.arguments = {"action": "ask"}; good.tool_calls = [call]
    once = AsyncMock(return_value=good)
    with patch.object(model_router, "_tool_call_once", new=once):
        await model_router.tool_call([{"role": "user", "content": "x"}], [MagicMock()],
                                     role="model_general", think=False)
    assert once.await_args.kwargs["think"] is False


def test_decide_switches_thinking_off():
    src = open("app/modules/assist_decide.py", encoding="utf-8").read()
    call = src[src.index("_RECORD_DECISION_TOOL],"):src.index("except Exception as exc:", src.index("_RECORD_DECISION_TOOL],"))]
    assert "think=False" in call


# ── state check: an argument from absence proposes nothing ──────────────────

def test_indirect_contradiction_is_flagged_not_proposed():
    verdicts = [
        {"id": "S:T12", "kind": "step", "node_key": "T12", "title": "Complete Jellyfin first-time setup",
         "verdict": "contradicted", "claim": "Jellyfin first-time setup completed: server name 'DeFruscio Media'",
         "reason": "grep found no references to 'DeFruscio Media' or 'AEDeFruscio'; output only lists data files.",
         "repair": "Complete the Jellyfin first-time setup wizard"},
        {"id": "F:9", "kind": "fact", "node_key": None, "verdict": "contradicted",
         "claim": "container uptime-kuma running", "reason": "docker ps: no rows", "repair": "Start the uptime-kuma container"},
    ]
    out = sc.proposals_from_verdicts(verdicts, anchor_node_key="ADD13")
    assert [(p["action"], p["node_key"]) for p in out] == [("repair", "ADD13")]
    assert verdicts[0]["needs_decision"] == "indirect"
    assert "look for yourself" in sc.render_verdicts(verdicts)
    assert "DIRECTLY shows" in sc._JUDGE_OPENING


@pytest.mark.parametrize("reason", [
    "output shows 'ide2: none,media=cdrom' instead of an attached ISO",
    "Active: failed (Result: exit-code)",
    "status: stopped",
])
def test_direct_contradictions_are_not_indirect(reason):
    assert not sc.indirect_contradiction({"reason": reason})


# ── tool-availability footer: another host's command is not this box's ──────

@pytest.mark.parametrize("line", [
    "ssh aedefruscio@192.168.1.127 nvidia-smi",
    "pct exec 111 -- systemctl status control-panel",
    "sudo pct enter 120",
    "qm guest exec 110 -- nvidia-smi",
    "docker exec -it app jq . /x.json",
    "kubectl exec -n ns pod -- jq .",
])
def test_remote_target_lines_are_skipped(line):
    assert assist_guide.runs_on_another_host(line), line
    hits = assist_guide.find_unavailable_tools(f"```bash\n{line}\n```", [{"tool": "nvidia-smi"}, {"tool": "jq"}])
    assert hits == [], line


def test_local_lines_are_still_flagged():
    hits = assist_guide.find_unavailable_tools("```bash\nnvidia-smi\njq . /tmp/x.json\n```", [{"tool": "nvidia-smi"}])
    assert [h["tool"] for h in hits] == ["nvidia-smi"]
    out, notes = assist_guide.repair_unavailable_tools(
        "```bash\nssh user@vm nvidia-smi\n```", {"missing_tools": [{"tool": "nvidia-smi"}], "profile": "root@pve"})
    assert notes == []


# ── the confirm nudge rides inside the fix ──────────────────────────────────

@pytest.mark.asyncio
async def test_blocked_submit_yields_offer_and_one_fix_with_the_nudge_inside():
    events = []
    async def run():
        async for ev in assist_turn._fix_flow("s1", "T5", "err", [], AsyncMock(),
                                              status_text="…", trailer="↩︎ Or — reply `confirm`."):
            events.append(ev)
    with patch("app.modules.assist_agent.run_step_fix", new=AsyncMock(return_value={"fix": "## Fix\nrun x"})), \
         patch("app.modules.assist_agent.capture_assistant_reply", new=AsyncMock()) as cap, \
         patch("app.modules.assist_agent._fix_failure_streak", new=AsyncMock(return_value=(0, None))):
        await run()
    answers = [d for n, d in events if n == "assist_answer"]
    assert len(answers) == 1 and answers[0]["kind"] == "fix"
    assert answers[0]["text"].rstrip().endswith("↩︎ Or — reply `confirm`.")
    assert cap.await_args.kwargs["content"] == answers[0]["text"]   # durable copy carries it too
    src = open("app/modules/assist_turn.py", encoding="utf-8").read()
    blocked = src[src.index("elif blocked_reason is not None:"):src.index('handled["v"] = "submit"')]
    assert "trailer=_nudge" in blocked and blocked.count("yield _ev(ASSIST_ANSWER") == 1  # offer only; no third bubble
