"""§17.868 — server-side turn loop unit tests.

Each test drives ``run_turn`` with the building blocks mocked and asserts the
EVENT SEQUENCE — the loop's whole contract is "a status frame before every
stage, the right dispatch, one terminal done frame".
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.modules import assist_turn

pytestmark = pytest.mark.asyncio

_SID = "11111111-2222-3333-4444-555555555555"


async def _collect(**kw):
    out = []
    async for name, data in assist_turn.run_turn(
        session_id=_SID, message=kw.get("message"), command=kw.get("command", "message"),
        node_key=kw.get("node_key"), history=[], db=AsyncMock(),
    ):
        out.append((name, data))
    return out


def _names(events):
    return [n for n, _ in events]


async def _fake_guide_stream(**_kw):
    yield {"type": "delta", "text": "step text"}
    yield {"type": "done", "status": "presented"}


def _guide_patches(node_key="T1"):
    sess = {"current_node_key": node_key, "status": "active",
            "step_counts": {"committed": 2, "pending": 3}}
    return (
        patch("app.modules.assist_agent.get_session", new=AsyncMock(return_value=sess)),
        patch("app.modules.assist_agent.generate_step_guidance_stream",
              new=_fake_guide_stream),
    )


async def test_guide_command_streams_walkthrough_and_done():
    p1, p2 = _guide_patches()
    with p1, p2:
        ev = await _collect(command="guide")
    names = _names(ev)
    assert "assist_guide_delta" in names and "assist_guide_done" in names
    assert names[-1] == "assist_turn_done"
    assert ev[-1][1]["handled"] == "guide"
    assert names.count("assist_turn_done") == 1


async def test_empty_message_is_single_done():
    ev = await _collect(message="   ")
    assert _names(ev) == ["assist_turn_done"]
    assert ev[-1][1]["handled"] == "empty"


async def test_whats_next_orients_without_decide():
    p1, p2 = _guide_patches()
    decide = AsyncMock()
    with p1, p2, \
         patch("app.modules.assist_agent.ingest_turn", new=AsyncMock()), \
         patch("app.modules.assist_decide.decide_turn", new=decide):
        ev = await _collect(message="whats next??")
    decide.assert_not_awaited()
    routed = [d for n, d in ev if n == "assist_turn_routed"]
    assert routed and routed[0]["override"] == "whats_next"
    # orientation status frame present, then guidance
    assert any("You're on step" in (d.get("text") or "")
               for n, d in ev if n == "assist_turn_status")
    assert ev[-1][1]["handled"] == "status"


async def test_note_dispatch_emits_note_and_proposal():
    note_res = {"recorded": True, "retracted_facts": ["f1"],
                "replan_proposal": {"proposals": [{"node_key": "T2"}]}}
    with patch("app.modules.assist_agent.ingest_turn", new=AsyncMock()), \
         patch("app.modules.assist_decide.decide_turn",
               new=AsyncMock(return_value={"action": "note", "confidence": "high",
                                           "note_kind": "constraint"})), \
         patch("app.routers.assist.assist_note", new=AsyncMock(return_value=note_res)):
        ev = await _collect(message="the box only has 16GB of ram")
    names = _names(ev)
    assert "assist_note_recorded" in names and "assist_replan_proposal" in names
    rec = next(d for n, d in ev if n == "assist_note_recorded")
    assert rec["kind"] == "constraint" and rec["retracted"] == 1
    assert ev[-1][1]["handled"] == "note"


async def test_submit_commits_then_claims_and_guides():
    p1, p2 = _guide_patches(node_key=None)  # no current → claim path
    nxt = {"node_key": "T5", "premise_check": {"stale": False}}
    with p1, p2, \
         patch("app.modules.assist_agent.ingest_turn", new=AsyncMock()), \
         patch("app.modules.assist_decide.decide_turn",
               new=AsyncMock(return_value={"action": "submit", "confidence": "high",
                                           "node_key": "T4", "evidence": "did it"})), \
         patch("app.routers.assist.assist_submit",
               new=AsyncMock(return_value={"status": "committed"})), \
         patch("app.routers.assist.assist_next", new=AsyncMock(return_value=nxt)):
        ev = await _collect(message="ran the command, all good, output attached")
    names = _names(ev)
    outcome = next(d for n, d in ev if n == "assist_step_outcome")
    assert outcome == {"node_key": "T4", "status": "committed"}
    assert "assist_guide_delta" in names  # walked into the next step
    assert ev[-1][1]["handled"] == "submit"


async def test_refused_submit_falls_back_without_killing_turn():
    with patch("app.modules.assist_agent.ingest_turn", new=AsyncMock()), \
         patch("app.modules.assist_decide.decide_turn",
               new=AsyncMock(return_value={"action": "submit", "confidence": "high",
                                           "node_key": "T4"})), \
         patch("app.routers.assist.assist_submit",
               new=AsyncMock(side_effect=RuntimeError("409 not claimable"))):
        ev = await _collect(message="output pasted here for the record ok")
    # no step_outcome, but the loop survived to its terminal frame
    assert "assist_step_outcome" not in _names(ev)
    assert ev[-1][0] == "assist_turn_done" and ev[-1][1]["handled"] == "submit"


async def test_ask_answers():
    with patch("app.modules.assist_agent.ingest_turn", new=AsyncMock()), \
         patch("app.modules.assist_decide.decide_turn",
               new=AsyncMock(return_value={"action": "ask", "confidence": "medium",
                                           "query": "what is jellyfin"})), \
         patch("app.modules.assist_agent.run_step_research",
               new=AsyncMock(return_value={"answer": "Jellyfin is a media server."})):
        ev = await _collect(message="what is jellyfin actually doing here")
    ans = next(d for n, d in ev if n == "assist_answer")
    assert ans["kind"] == "ask" and "media server" in ans["text"]
    assert ev[-1][1]["handled"] == "ask"


async def test_low_confidence_falls_back_to_track_then_guide():
    p1, p2 = _guide_patches()
    with p1, p2, \
         patch("app.modules.assist_agent.ingest_turn", new=AsyncMock()), \
         patch("app.modules.assist_decide.decide_turn",
               new=AsyncMock(return_value={"action": "note", "confidence": "low"})), \
         patch("app.routers.assist.assist_track",
               new=AsyncMock(return_value={"action": "on_step"})):
        ev = await _collect(message="hmm the thing did a thing i guess")
    names = _names(ev)
    assert "assist_note_recorded" not in names  # low-conf note NOT dispatched
    assert "assist_guide_delta" in names        # fell through to guidance
    assert ev[-1][1]["handled"] == "fallback"


async def test_decide_crash_still_reaches_fallback_guidance():
    p1, p2 = _guide_patches()
    with p1, p2, \
         patch("app.modules.assist_agent.ingest_turn", new=AsyncMock()), \
         patch("app.modules.assist_decide.decide_turn",
               new=AsyncMock(side_effect=RuntimeError("model down"))), \
         patch("app.routers.assist.assist_track",
               new=AsyncMock(return_value={"action": "on_step"})):
        ev = await _collect(message="here is some output from the run today")
    assert "assist_guide_delta" in _names(ev)
    assert ev[-1][0] == "assist_turn_done"


# ── §17.869 — detached turn runs ─────────────────────────────────────────────


def _fake_async_session(rows):
    """Context-manager factory yielding a db whose execute() pops from rows."""
    from unittest.mock import MagicMock

    def _mk():
        db = AsyncMock()

        async def _exec(sql, params=None):
            r = rows.pop(0)
            m = MagicMock()
            m.mappings.return_value.first.return_value = r
            m.scalar.return_value = r.get("_scalar") if isinstance(r, dict) else r
            return m
        db.execute = AsyncMock(side_effect=_exec)
        db.commit = AsyncMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=db)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm
    return _mk


async def test_tail_replays_then_follows_until_done():
    """A tail must (1) emit assist_turn_started, (2) replay frames from 0,
    (3) keep polling while running, (4) end when the run finishes — this is
    what makes a browser reload lossless."""
    rows = [
        {"status": "running", "frames": [{"e": "assist_turn_status", "d": {"text": "a"}}]},
        {"status": "done", "frames": [
            {"e": "assist_turn_status", "d": {"text": "a"}},
            {"e": "assist_turn_done", "d": {"handled": "x"}},
        ]},
    ]
    with patch("app.database.async_session", new=_fake_async_session(rows)), \
         patch("asyncio.sleep", new=AsyncMock()):
        out = []
        async for ev in assist_turn.tail_turn_run("run-1"):
            out.append(ev)
    names = [n for n, _ in out]
    assert names[0] == "assist_turn_started"
    assert names[1:] == ["assist_turn_status", "assist_turn_done"]  # no re-replay


async def test_tail_missing_run_errors_cleanly():
    with patch("app.database.async_session", new=_fake_async_session([None])):
        out = [ev async for ev in assist_turn.tail_turn_run("nope")]
    assert out[-1][0] == "error" and "not found" in out[-1][1]["detail"]


async def test_fix_dispatch_is_research_backed():
    """§17.874 — fixes research, unconditionally: consecutive fixes cycled
    guessed repo URLs while live research would have supplied the current
    correct instructions (the operator's standing unsure→research requirement)."""
    fixer = AsyncMock(return_value={"fix": "researched fix"})
    with patch("app.modules.assist_agent.ingest_turn", new=AsyncMock()), \
         patch("app.modules.assist_agent.capture_assistant_reply", new=AsyncMock()), \
         patch("app.modules.assist_decide.decide_turn",
               new=AsyncMock(return_value={"action": "fix", "confidence": "high",
                                           "node_key": "T14", "error_text": "boom"})), \
         patch("app.modules.assist_agent.run_step_fix", new=fixer):
        ev = await _collect(message="root@pve:~# apt-get update failed with an error")
    assert fixer.await_args.kwargs.get("research") is True
    ans = next(d for n, d in ev if n == "assist_answer")
    assert ans["text"] == "researched fix"


# ── §17.875 — zombie sweep + tail stall cap ──────────────────────────────────


async def test_sweep_zombie_runs_marks_dead():
    """A restart leaves 'running' rows forever; the boot sweep must mark them
    error with an honest terminal frame (so tails end and resume skips them)."""
    from unittest.mock import MagicMock

    executed = {}

    def _mk():
        db = AsyncMock()

        async def _exec(sql, params=None):
            executed["sql"] = str(sql)
            executed["frames"] = params.get("f") if params else None
            m = MagicMock()
            m.rowcount = 3
            return m
        db.execute = AsyncMock(side_effect=_exec)
        db.commit = AsyncMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=db)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    with patch("app.database.async_session", new=_mk):
        n = await assist_turn.sweep_zombie_runs()
    assert n == 3
    assert "status = 'running'" in executed["sql"]
    assert "restarted mid-turn" in executed["frames"]


async def test_tail_stall_cap_releases_screen():
    """A 'running' row that never grows must not be followed forever — the
    tail ends with an honest error + done after the stall window."""
    from unittest.mock import MagicMock

    rows = [{"status": "running",
             "frames": [{"e": "assist_turn_status", "d": {"text": "a"}}]}] * 50

    clock = {"t": 1000.0}

    def _now():
        clock["t"] += 100.0  # every poll advances 100s → stall cap hit fast
        return clock["t"]

    loop = MagicMock()
    loop.time = _now
    with patch("app.database.async_session", new=_fake_async_session(rows)), \
         patch("asyncio.sleep", new=AsyncMock()), \
         patch("asyncio.get_event_loop", return_value=loop):
        out = [ev async for ev in assist_turn.tail_turn_run("wedged")]
    names = [n for n, _ in out]
    assert names[-1] == "assist_turn_done"
    assert out[-1][1]["handled"] == "stalled_tail"
    assert any("gone quiet" in (d.get("detail") or "") for n, d in out if n == "error")


# ── §17.878 — unclaimed-step self-heal ───────────────────────────────────


async def test_submit_must_claim_first_selfheals_claims_and_retries():
    """Live incident: successful install evidence refused with 409
    must_claim_first (pointer moved without a claim; presented_at NULL).
    The loop must claim via assist_next and retry ONCE, then commit."""
    from fastapi import HTTPException
    p1, p2 = _guide_patches(node_key=None)
    refusal = HTTPException(status_code=409, detail={
        "error_code": "must_claim_first",
        "message": "step T14 is pending; claim it first"})
    submit = AsyncMock(side_effect=[refusal, {"status": "committed"}])
    nxt = AsyncMock(return_value={"node_key": "T14", "premise_check": {}})
    with p1, p2, \
         patch("app.modules.assist_agent.ingest_turn", new=AsyncMock()), \
         patch("app.modules.assist_decide.decide_turn",
               new=AsyncMock(return_value={"action": "submit", "confidence": "high",
                                           "node_key": "T14", "evidence": "active + 200"})), \
         patch("app.routers.assist.assist_submit", new=submit), \
         patch("app.routers.assist.assist_next", new=nxt):
        ev = await _collect(message="systemctl is-active prowlarr -> active, curl 200")
    assert submit.await_count == 2
    nxt.assert_awaited()
    outcome = next(d for n, d in ev if n == "assist_step_outcome")
    assert outcome["status"] == "committed"
    assert any("claiming it now" in (d.get("text") or "").lower()
               for n, d in ev if n == "assist_turn_status")
    assert ev[-1][1]["handled"] == "submit"


async def test_submit_selfheal_second_refusal_falls_back():
    """If the retry ALSO fails, keep the §17.863 explain-and-continue path."""
    from fastapi import HTTPException
    refusal = HTTPException(status_code=409, detail={"error_code": "must_claim_first",
                                                     "message": "step T14 is pending"})
    submit = AsyncMock(side_effect=[refusal, refusal])
    with patch("app.modules.assist_agent.ingest_turn", new=AsyncMock()), \
         patch("app.modules.assist_decide.decide_turn",
               new=AsyncMock(return_value={"action": "submit", "confidence": "high",
                                           "node_key": "T14"})), \
         patch("app.routers.assist.assist_submit", new=submit), \
         patch("app.routers.assist.assist_next", new=AsyncMock(return_value={})):
        ev = await _collect(message="output pasted here for the record ok")
    assert submit.await_count == 2
    assert "assist_step_outcome" not in _names(ev)
    assert ev[-1][0] == "assist_turn_done"


async def test_claim_and_guide_repairs_pending_pointer_step():
    """§17.878 layer 2: a 'pending' pointer step is claimed at the guide
    chokepoint so the mirror invariant holds before any walkthrough."""
    from unittest.mock import MagicMock
    p1, p2 = _guide_patches(node_key="T14")
    nxt = AsyncMock(return_value={"node_key": "T14"})
    db = AsyncMock()
    probe = MagicMock()
    probe.scalar.return_value = "pending"
    db.execute = AsyncMock(return_value=probe)
    with p1, p2, patch("app.routers.assist.assist_next", new=nxt):
        out = []
        async for e in assist_turn._claim_and_guide(_SID, "T14", [], db, orient=False):
            out.append(e)
    nxt.assert_awaited_once()
    assert any(n == "assist_guide_done" for n, _ in out)
