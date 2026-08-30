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
