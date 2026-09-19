"""§17.1109 (Phase 1 ledger L-1) — per-stage timing for one assist turn.

Covers the pure timer, the frame timestamps, the driver writing the record at
finalize, and the call recorder defaulting ``call_kind`` to the active stage.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

from app.utils import turn_timing
from app.utils.turn_timing import TurnTimer, current_turn_timer, default_call_kind, slug


class _Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


# ── the timer ────────────────────────────────────────────────────────────────

def test_stages_accrue_wall_time_and_llm_time_to_the_open_stage():
    clk = _Clock()
    t = TurnTimer(clock=clk)
    clk.advance(0.5)
    t.mark("Reading that…")
    clk.advance(2.0)
    t.note_llm(1500)
    t.mark("Deciding how to act on that…")
    clk.advance(10.0)
    t.note_llm(4000)
    t.note_llm(3000)
    clk.advance(1.0)
    res = t.finish()

    labels = [s["label"] for s in res["stages"]]
    assert labels == ["start", "Reading that…", "Deciding how to act on that…"]
    by = {s["label"]: s for s in res["stages"]}
    assert by["start"]["ms"] == 500 and by["start"]["llm_ms"] == 0
    assert by["Reading that…"]["ms"] == 2000 and by["Reading that…"]["llm_ms"] == 1500
    assert by["Reading that…"]["non_llm_ms"] == 500
    assert by["Deciding how to act on that…"]["ms"] == 11000
    assert by["Deciding how to act on that…"]["llm_ms"] == 7000
    assert by["Deciding how to act on that…"]["llm_calls"] == 2
    assert res["total_ms"] == 13500 and res["llm_ms"] == 8500 and res["llm_calls"] == 3
    assert res["non_llm_ms"] == 5000
    assert "Deciding how to act on that…"[:60] == by["Deciding how to act on that…"]["label"]


def test_summary_line_names_every_stage_with_ms_and_llm_ms():
    clk = _Clock()
    t = TurnTimer(clock=clk)
    t.mark("Reading that…"); clk.advance(1.0); t.note_llm(400)
    line = t.summary_line(t.finish())
    assert "total_ms=1000" in line and "llm_ms=400" in line and "llm_calls=1" in line
    assert "reading_that:1000/400" in line


def test_annotations_ride_the_record():
    t = TurnTimer(clock=_Clock())
    t.annotate("action", "fix")
    assert t.finish()["annotations"] == {"action": "fix"}


def test_slug_is_log_safe():
    assert slug("Deciding how to act on that…") == "deciding_how_to_act_on_that"
    assert slug("") == "stage"


def test_default_call_kind_follows_the_open_stage_and_is_none_outside_a_turn():
    assert default_call_kind() is None
    t = TurnTimer(clock=_Clock())
    token = current_turn_timer.set(t)
    try:
        t.mark("Recording the result and verifying the step…")
        assert default_call_kind() == "assist:recording_the_result_and_verifying_the_step"
        turn_timing.note_llm_call(250)
        assert t.llm_calls == 1 and t.llm_ms == 250
    finally:
        current_turn_timer.reset(token)
    assert default_call_kind() is None
    turn_timing.note_llm_call(999)          # no-op outside a turn, must not raise


# ── the driver ───────────────────────────────────────────────────────────────

async def test_append_frames_stamps_t_when_given_stamps():
    from app.modules import assist_turn
    seen: dict = {}

    class Db:
        async def execute(self, stmt, params=None):
            seen["payload"] = json.loads(params["f"])

        async def commit(self):
            pass

    await assist_turn._append_frames("r1", [("assist_turn_status", {"text": "a"}), ("x", {})], Db(), stamps=[12, 340])
    assert seen["payload"] == [{"e": "assist_turn_status", "d": {"text": "a"}, "t": 12}, {"e": "x", "d": {}, "t": 340}]
    await assist_turn._append_frames("r1", [("x", {})], Db())          # legacy shape, no stamps
    assert seen["payload"] == [{"e": "x", "d": {}}]


async def test_drive_turn_run_writes_the_timing_record_at_finalize():
    """Status frames become stages; the finalize UPDATE carries ``timings``
    and the log line names the stages."""
    from app.modules import assist_turn

    async def fake_run_turn(**kw):
        yield ("assist_turn_status", {"text": "Reading that…"})
        yield ("assist_turn_routed", {"action": "fix", "override": None})
        yield ("assist_turn_status", {"text": "Deciding how to act on that…"})
        yield ("assist_turn_done", {"handled": "fix"})

    finalize: dict = {}

    class Db:
        async def execute(self, stmt, params=None):
            if "finished_at" in str(stmt):
                finalize.update(params)
                assert "timings = CAST(:tm AS jsonb)" in str(stmt)

        async def commit(self):
            pass

    class Ctx:
        async def __aenter__(self):
            return Db()

        async def __aexit__(self, *a):
            return False

    appended: list = []

    async def fake_append(run_id, frames, db, stamps=None):
        appended.append((list(frames), list(stamps or [])))

    with patch("app.database.async_session", lambda: Ctx()), \
         patch.object(assist_turn, "run_turn", fake_run_turn), \
         patch.object(assist_turn, "_append_frames", fake_append):
        await assist_turn._drive_turn_run(run_id="r7", session_id="s", message="hi",
                                          command="message", node_key=None, history=[])

    tm = json.loads(finalize["tm"])
    assert finalize["st"] == "done"
    labels = [s["label"] for s in tm["stages"]]
    assert labels == ["start", "Reading that…", "Deciding how to act on that…"]
    assert tm["annotations"] == {"action": "fix"}
    assert tm["llm_calls"] == 0 and tm["total_ms"] >= 0
    # every appended frame carried a stamp
    assert appended and all(len(f) == len(s) for f, s in appended)
    assert current_turn_timer.get() is None, "the ContextVar must be reset after the turn"


# ── the call recorder ────────────────────────────────────────────────────────

async def test_record_llm_call_defaults_call_kind_to_the_active_stage_and_accrues():
    from app.utils import cost_tracking

    captured: dict = {}

    class Db:
        async def execute(self, stmt, params=None):
            captured.update(params or {})

        async def commit(self):
            pass

    class Ctx:
        async def __aenter__(self):
            return Db()

        async def __aexit__(self, *a):
            return False

    resp = MagicMock(provider="ollama", model="m", tokens_prompt=1, tokens_completion=2,
                     total_duration_ms=1234, success=True)
    t = TurnTimer(clock=_Clock())
    t.mark("Deciding how to act on that…")
    token = current_turn_timer.set(t)
    try:
        with patch("app.database.async_session", lambda: Ctx()), \
             patch.object(cost_tracking, "compute_cost_usd", AsyncMock(return_value=0.0)):
            await cost_tracking.record_llm_call(resp)
    finally:
        current_turn_timer.reset(token)
    assert captured["call_kind"] == "assist:deciding_how_to_act_on_that"
    assert t.llm_calls == 1 and t.llm_ms == 1234


async def test_record_llm_call_keeps_an_explicit_call_kind():
    from app.utils import cost_tracking

    captured: dict = {}

    class Db:
        async def execute(self, stmt, params=None):
            captured.update(params or {})

        async def commit(self):
            pass

    class Ctx:
        async def __aenter__(self):
            return Db()

        async def __aexit__(self, *a):
            return False

    resp = MagicMock(provider="ollama", model="m", tokens_prompt=1, tokens_completion=2,
                     total_duration_ms=10, success=True)
    t = TurnTimer(clock=_Clock()); t.mark("x")
    token = current_turn_timer.set(t)
    try:
        with patch("app.database.async_session", lambda: Ctx()), \
             patch.object(cost_tracking, "compute_cost_usd", AsyncMock(return_value=0.0)), \
             cost_tracking.call_kind("model_ab"):
            await cost_tracking.record_llm_call(resp)
    finally:
        current_turn_timer.reset(token)
    assert captured["call_kind"] == "model_ab"
