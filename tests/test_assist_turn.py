"""§17.868 — server-side turn loop unit tests.

Each test drives ``run_turn`` with the building blocks mocked and asserts the
EVENT SEQUENCE — the loop's whole contract is "a status frame before every
stage, the right dispatch, one terminal done frame".
"""
from __future__ import annotations

import asyncio
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
    assert outcome["node_key"] == "T4" and outcome["status"] == "committed"
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
    # no step_outcome; §17.889(#11): a DURABLE answer explains the refusal
    assert "assist_step_outcome" not in _names(ev)
    assert any("wouldn't accept" in (d.get("text") or "")
               for n, d in ev if n == "assist_answer")
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
    assert any("wouldn't accept" in (d.get("text") or "")
               for n, d in ev if n == "assist_answer")  # §17.889(#11)
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


# ── §17.880 — terminal-pointer heal (Guide/Done must not replay a done step) ─


async def test_claim_and_guide_heals_terminal_pointer_forward():
    """Live incident: tracker retired T14 but the pointer stayed on it — every
    Guide/Done press re-walked the finished step. A terminal pointer must
    announce + heal into the claim path (next step claimed and walked)."""
    from unittest.mock import MagicMock
    p1, p2 = _guide_patches(node_key="T14")
    nxt = AsyncMock(return_value={"node_key": "T15", "premise_check": {}})
    db = AsyncMock()
    probe = MagicMock()
    probe.scalar.return_value = "committed"
    db.execute = AsyncMock(return_value=probe)
    with p1, p2, patch("app.routers.assist.assist_next", new=nxt):
        out = []
        async for e in assist_turn._claim_and_guide(_SID, "T14", [], db, orient=False):
            out.append(e)
    nxt.assert_awaited_once()
    assert any("already done" in (d.get("text") or "")
               for n, d in out if n == "assist_turn_status")
    done = [d for n, d in out if n == "assist_guide_done"]
    assert done and done[0].get("node_key") == "T15"


async def test_claim_and_guide_live_pointer_untouched():
    """A live (presented) pointer step is guided as-is — no heal, no claim."""
    from unittest.mock import MagicMock
    p1, p2 = _guide_patches(node_key="T14")
    nxt = AsyncMock()
    db = AsyncMock()
    probe = MagicMock()
    probe.scalar.return_value = "presented"
    db.execute = AsyncMock(return_value=probe)
    with p1, p2, patch("app.routers.assist.assist_next", new=nxt):
        out = []
        async for e in assist_turn._claim_and_guide(_SID, "T14", [], db, orient=False):
            out.append(e)
    nxt.assert_not_awaited()
    done = [d for n, d in out if n == "assist_guide_done"]
    assert done and done[0].get("node_key") == "T14"


async def test_retire_step_mirrored_advances_pointer():
    """§17.880 root cause: the tracker's retire path must move the session
    pointer to the next claimable step, like the submit-commit path does."""
    from app.routers.assist import _retire_step_mirrored
    db = AsyncMock()
    with patch("app.routers.assist.assist_agent._next_pending_node_key",
               new=AsyncMock(return_value="T15")):
        await _retire_step_mirrored(
            db=db, job_id="j", session_id=_SID, node_key="T14", evidence="done")
    updates = [str(c.args[0]) for c in db.execute.await_args_list]
    assert any("UPDATE assist_sessions SET current_node_key" in u for u in updates)
    ptr_call = next(c for c in db.execute.await_args_list
                    if "current_node_key" in str(c.args[0]))
    assert ptr_call.args[1]["nk"] == "T15"
    step_update = next(u for u in updates if "assist_steps" in u)
    assert "committed_at=NOW()" in step_update


# ── §17.884 — blocked submit continues into the fix flow ─────────────────


async def test_incomplete_submit_continues_into_fix():
    """Live incident: operator pasted the discovery output the engine asked
    for; verifier said step_incomplete; the turn DEAD-ENDED. A blocked submit
    must continue into a fix seeded with the evidence + verifier reason."""
    fix_res = {"fix": "Now download using the URL from your output: curl -L <that url> -o /tmp/R.tar.gz"}
    submit_res = {"status": "step_incomplete",
                  "success_verdict": {"outcome": "incomplete",
                                      "reason": "URL identified but Radarr not installed"}}
    captured_error = {}
    async def fake_fix(**kw):
        captured_error.update(kw)
        return fix_res
    with patch("app.modules.assist_agent.ingest_turn", new=AsyncMock()), \
         patch("app.modules.assist_decide.decide_turn",
               new=AsyncMock(return_value={"action": "submit", "confidence": "high",
                                           "node_key": "T16", "evidence": "found the url"})), \
         patch("app.routers.assist.assist_submit",
               new=AsyncMock(return_value=submit_res)), \
         patch("app.modules.assist_agent.run_step_fix", new=fake_fix), \
         patch("app.modules.assist_agent.capture_assistant_reply", new=AsyncMock()):
        ev = await _collect(message="browser_download_url: https://github.com/R/R/releases/download/v5.27.5.10198/x.tar.gz")
    names = _names(ev)
    outcome = next(d for n, d in ev if n == "assist_step_outcome")
    assert outcome["status"] == "step_incomplete"
    answers = [d for n, d in ev if n == "assist_answer"]
    # §17.884 — the continuation fix is still emitted, seeded with the evidence.
    assert any("curl -L" in a["text"] for a in answers)
    # §17.951 — and the completion OFFER now LEADS it. Ordering is the point:
    # the operator reads the top of the reply, so "if it IS done, reply
    # `confirm`" has to arrive before the fix, not under it.
    assert "reply `confirm`" in answers[0]["text"]
    assert "curl -L" in answers[1]["text"]
    assert "not complete" in captured_error["error"]
    assert "Radarr not installed" in captured_error["error"]
    assert ev[-1][1]["handled"] == "submit"


async def test_committed_submit_still_advances_not_fixes():
    """A committed submit keeps the §17.868 claim-and-guide advance."""
    p1, p2 = _guide_patches(node_key=None)
    nxt = {"node_key": "T17", "premise_check": {}}
    fix = AsyncMock()
    with p1, p2, \
         patch("app.modules.assist_agent.ingest_turn", new=AsyncMock()), \
         patch("app.modules.assist_decide.decide_turn",
               new=AsyncMock(return_value={"action": "submit", "confidence": "high",
                                           "node_key": "T16", "evidence": "done"})), \
         patch("app.routers.assist.assist_submit",
               new=AsyncMock(return_value={"status": "committed"})), \
         patch("app.routers.assist.assist_next", new=AsyncMock(return_value=nxt)), \
         patch("app.modules.assist_agent.run_step_fix", new=fix):
        ev = await _collect(message="all done, service active")
    fix.assert_not_awaited()
    assert "assist_guide_delta" in _names(ev)


# ── §17.885 — the tracker must ACTUALLY run on the fallback path ─────────


async def test_fallback_path_actually_invokes_tracker():
    """Audit finding: the fallback imported a NONEXISTENT input class since
    §17.868; the swallowed ImportError made the tracker a silent no-op and the
    old test passed because it never asserted the tracker was CALLED. This one
    does — and uses the REAL input class import path."""
    p1, p2 = _guide_patches(node_key="T9")
    track = AsyncMock(return_value={"action": "proceed"})
    with p1, p2, \
         patch("app.modules.assist_agent.ingest_turn", new=AsyncMock()), \
         patch("app.modules.assist_decide.decide_turn",
               new=AsyncMock(return_value={"action": "unknown", "confidence": "low"})), \
         patch("app.routers.assist.assist_track", new=track):
        ev = await _collect(message="ok that box is racked and cabled now moving on")
    track.assert_awaited_once()   # ImportError would leave this un-awaited
    body = track.await_args.args[1]
    assert body.message.startswith("ok that box")
    assert "assist_guide_delta" in _names(ev)


async def test_add_step_with_reshape_tag_dispatches_add_step_not_note():
    """§17.1053b — live: the model routed "add a step for this" to add_step
    AND tagged plan_impact=reshape; the reshape branch ran first and filed a
    note. add_step IS the plan change — it must dispatch."""
    p1, p2 = _guide_patches(node_key="ADD1")
    added = {"node_key": "ADD1", "title": "Repair the Caddyfile in LXC 120",
             "steps": [{"node_key": "ADD1", "title": "Repair the Caddyfile in LXC 120"}], "count": 1}
    with p1, p2, \
         patch("app.modules.assist_agent.ingest_turn", new=AsyncMock()), \
         patch("app.modules.assist_decide.decide_turn",
               new=AsyncMock(return_value={"action": "add_step", "confidence": "high",
                                           "plan_impact": "reshape", "node_key": "T37"})), \
         patch("app.routers.assist.assist_add_step", new=AsyncMock(return_value=added)) as add, \
         patch("app.routers.assist.assist_note", new=AsyncMock()) as note:
        ev = await _collect(message="add a step for this")
    names = _names(ev)
    add.assert_awaited_once()
    note.assert_not_awaited()
    assert "assist_note_recorded" not in names
    assert ev[-1][1]["handled"] == "add_step"
    assert any("Added a step" in (d.get("text") or "") for n, d in ev if n == "assist_turn_status")


# ── §17.1082 — liveness: tail pulse, deep-code progress notes, retry surfacing ──


async def test_tail_pulses_while_quiet_and_running(monkeypatch):
    """Rows that stay 'running' with no new frame: after _PULSE_AFTER_S the
    tail emits assist_turn_pulse every _PULSE_EVERY_S (not persisted), then
    ends normally when the run finishes. Live: a 105 s quiet research pass
    looked like a dead page."""
    quiet = {"status": "running", "frames": [{"e": "assist_turn_status", "d": {"text": "working"}}]}
    done = {"status": "done", "frames": quiet["frames"] + [{"e": "assist_turn_done", "d": {"handled": "x"}}]}
    rows = [quiet] * 6 + [done]
    clock = {"t": 1000.0}
    loop = asyncio.get_event_loop()
    monkeypatch.setattr(type(loop), "time", lambda self: clock["t"])
    async def tick(_):
        clock["t"] += 5.0            # each poll = 5 s of wall clock
    with patch("app.database.async_session", new=_fake_async_session(rows)), \
         patch("asyncio.sleep", new=tick):
        out = [ev async for ev in assist_turn.tail_turn_run("run-p")]
    names = [n for n, _ in out]
    pulses = [d for n, d in out if n == "assist_turn_pulse"]
    assert names[0] == "assist_turn_started" and names[1] == "assist_turn_status" and names[-1] == "assist_turn_done"
    assert len(pulses) >= 2                                  # 30 s quiet → pulses at 10 s and 20 s (and 30 s)
    assert pulses[0]["quiet_s"] >= assist_turn._PULSE_AFTER_S and pulses[0]["running_s"] >= pulses[0]["quiet_s"]
    assert pulses[1]["quiet_s"] - pulses[0]["quiet_s"] >= assist_turn._PULSE_EVERY_S


def test_turn_note_sink_is_task_local_and_never_raises():
    from app.utils import progress as pg
    assert pg.turn_note("nobody listening") is None           # no sink → no-op
    got = []
    tok = pg.set_turn_note_sink(got.append)
    try:
        pg.turn_note("retrying")
        assert got == ["retrying"]
        pg.set_turn_note_sink(lambda t: 1 / 0)
        pg.turn_note("boom")                                  # a broken sink is swallowed
    finally:
        pg.reset_turn_note_sink(tok)
    assert pg.turn_note("after reset") is None


async def test_driver_installs_the_sink_and_notes_land_on_the_run_row(monkeypatch):
    """The driver wraps the loop with a sink that appends assist_turn_status
    frames through a DETACHED session, and drains those tasks before
    finalizing the row."""
    appended = []
    async def fake_note(run_id, text_value):
        appended.append((run_id, text_value))
    monkeypatch.setattr(assist_turn, "_append_note_detached", fake_note)
    async def fake_run_turn(**kw):
        from app.utils.progress import turn_note
        turn_note("The model call to m failed (HTTP 500) — retrying, attempt 2 of 3…")
        yield ("assist_turn_done", {"handled": "x"})
    monkeypatch.setattr(assist_turn, "run_turn", fake_run_turn)
    with patch("app.database.async_session", new=_fake_async_session([None] * 5)), \
         patch.object(assist_turn, "_append_frames", new=AsyncMock()):
        await assist_turn._drive_turn_run(run_id="r9", session_id="s", message=None, command="guide", node_key=None, history=[])
    assert appended == [("r9", "The model call to m failed (HTTP 500) — retrying, attempt 2 of 3…")]
    from app.utils import progress as pg
    assert pg._TURN_NOTE_SINK.get() is None                   # reset after the turn


def test_router_retry_says_so_in_the_status_line():
    from app import model_router as mr
    assert mr._short_error("HTTP 500: {\"StatusCode\":500}") == "HTTP 500"
    assert mr._short_error("x" * 100).endswith("…") and len(mr._short_error("x" * 100)) == 61
    assert mr._short_error(None) == "no detail"
    src = open(mr.__file__, encoding="utf-8").read()
    block = src[src.index("Attempt %d/%d failed for %s"):src.index("# Phase 2: fallback")]
    assert "turn_note(" in block and "retrying, " in block and "attempt {attempt + 2} of {retries}" in block
    assert block.index('if classification == "fail_fast":') < block.index("turn_note(")   # no note on a fail-fast


def test_pulse_event_is_registered_and_vendored():
    from app import sse_events
    assert sse_events.ASSIST_TURN_PULSE == "assist_turn_pulse"
    import pathlib
    root = pathlib.Path(assist_turn.__file__).resolve().parents[2]
    assert "ASSIST_TURN_PULSE" in (root / "pipelines" / "_vendor" / "_sse_events.py").read_text(encoding="utf-8")
    js = (root / "app" / "ui" / "static" / "views" / "assist.js").read_text(encoding="utf-8")
    for needle in ('case "assist_turn_pulse"', "working ${fmtElapsed", "no word from the engine", "closed before this turn finished", "maybeResumeActiveTurn(true)"):
        assert needle in js, needle
