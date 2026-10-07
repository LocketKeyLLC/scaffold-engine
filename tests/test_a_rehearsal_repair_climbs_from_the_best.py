"""§17.1411 — a rehearsal-refused draft climbs from its BEST attempt, and the best is remembered.

Live, 2026-10-07. With §17.1409 rehearsing every ADD122 draft, three reask rounds went:
"122 -> 122 settings, only the `[section]` header lost", then a missing directory, then a
Python crash. The redraft chain ranks by refusal COUNT (always one), stops when the
refusal KIND repeats (always the rehearsal's), and never shows a draft its predecessor --
so each round threw the near-miss away and started from nothing.
"""
from __future__ import annotations

import inspect
import json
import pathlib

import pytest

from app.modules import execution_agent
from app.modules import rehearsal as rh

FIX = pathlib.Path(__file__).parent / "fixtures" / "rehearsal"
GOOD = json.loads((FIX / "report_good_route.json").read_text())
CORRUPT = json.loads((FIX / "report_corrupts_the_file.json").read_text())       # 122 -> 1, header lost
WRONG_UNIT = json.loads((FIX / "report_wrong_unit.json").read_text())          # a command failed, PUT 400


def _report(**rt):
    return {"commands": [], "roundtrip": {"ok": False, "get_status": 200, "put_status": 200, **rt}}


# ── the evidence ranks the attempts ──────────────────────────────────────────

def test_a_pass_is_zero_and_everything_else_is_more():
    assert rh.score(GOOD) == 0
    assert rh.score(CORRUPT) > 0 and rh.score(WRONG_UNIT) > 0


def test_the_live_near_miss_outranks_the_live_regressions():
    """Round 1 (122 -> 122, header lost) must beat round 2 (a command failed, GET 404)
    and a crash on PUT -- the order the engine threw away."""
    near = _report(header_kept=False, expected_keys=122, got_keys=122)
    crash_on_put = _report(put_status=500, header_kept=False, expected_keys=122, got_keys=0)
    no_route = {"commands": [{"exit": 1}], "roundtrip": {"ok": False, "get_status": 404}}
    assert rh.score(near) < rh.score(crash_on_put) < rh.score(no_route)


def test_the_real_reports_rank_sensibly():
    assert rh.score(GOOD) < rh.score(CORRUPT) < rh.score(WRONG_UNIT)


@pytest.mark.parametrize("bad", [None, {"error": "rehearsal exceeded 240s"}])
def test_an_unscored_report_is_never_mistaken_for_close(bad):
    assert rh.score(bad) >= 10_000


# ── the repair note shows the draft itself ───────────────────────────────────

def test_the_repair_note_carries_the_draft_verbatim_and_the_evidence():
    note = rh.repair_note("## Run this\n```bash\nbash /tmp/x.sh\n```", "the file lost its `[section]` header")
    assert "bash /tmp/x.sh" in note and "lost its `[section]` header" in note
    assert "Do not rewrite it from scratch" in note


def test_a_long_draft_is_cut_and_says_so():
    note = rh.repair_note("x" * 20_000, "why")
    assert "(truncated)" in note and len(note) < 18_000


# ── the best is remembered across pauses ─────────────────────────────────────

class _FakeDB:
    def __init__(self):
        self.meta: dict = {"other": 1}

    async def execute(self, stmt, params=None):
        params = params or {}

        class _R:
            def __init__(s, v):
                s.v = v

            def scalar(s):
                return s.v
        if "SELECT metadata" in str(stmt):
            return _R(json.dumps(self.meta))
        patch = json.loads(params["patch"])
        self.meta = {**self.meta, **patch}
        return _R(None)

    async def commit(self):
        pass


@pytest.mark.asyncio
async def test_remember_and_recall_and_forget():
    db = _FakeDB()
    assert await rh.best_so_far(db, "j", "ADD122") is None
    await rh.remember_best(db, "j", "ADD122", {"runbook": "rb", "score": 70, "why": "header"})
    assert (await rh.best_so_far(db, "j", "ADD122"))["score"] == 70
    assert db.meta["other"] == 1, "the rest of the job's metadata is untouched"
    await rh.remember_best(db, "j", "ADD123", {"runbook": "rb2", "score": 9, "why": ""})
    await rh.remember_best(db, "j", "ADD122", None)
    assert await rh.best_so_far(db, "j", "ADD122") is None
    assert (await rh.best_so_far(db, "j", "ADD123"))["runbook"] == "rb2"


def test_the_memory_never_binds_a_parameter_as_a_jsonb_key():
    src = inspect.getsource(rh.remember_best)
    assert "CAST(:patch AS jsonb)" in src and "jsonb_build_object(:" not in src and "ARRAY[:" not in src


# ── wired after the redraft chain ────────────────────────────────────────────

def test_the_repair_loop_runs_after_the_chain_and_keeps_the_best():
    src = inspect.getsource(execution_agent._pause_for_decision)
    i_chain = src.index("supervised_run_redraft_last_rejected")
    i_loop = src.index("for _attempt in range(rehearsal.REPAIRS):")
    i_nothing = src.index("§17.1227 — a draft with NOTHING to run")
    assert i_chain < i_loop < i_nothing
    assert "rehearsal.repair_note(_best[\"runbook\"], _best[\"why\"])" in src
    assert "rehearsal.remember_best(" in src and "rehearsal.best_so_far(" in src
    assert "_rh_reports[rb] = report" in src
    assert rh.REPAIRS >= 3


# ── §17.1411b — the evidence says what the block DID ─────────────────────────

NOOP = json.loads((FIX / "report_edit_changed_nothing.json").read_text())
GOOD_DIFF = json.loads((FIX / "report_good_route_with_diff.json").read_text())


def test_an_edit_that_changed_nothing_is_said():
    """Live: five repairs kept a `sed` that matched no line of server.js, told only
    "GET 404" each time."""
    assert "/opt/control-panel-backend/server.js" in NOOP["unchanged_files"]
    why = rh.refusal_from(NOOP)[0]["why"]
    assert "EXACTLY as before" in why and "changed nothing" in why
    assert "`/opt/control-panel-backend/server.js`" in why


def test_a_good_route_reports_its_diff_and_passes():
    assert [c["path"] for c in GOOD_DIFF["changed_files"]] == ["/opt/control-panel-backend/server.js"]
    assert "routes/good" in GOOD_DIFF["changed_files"][0]["diff"]
    assert rh.refusal_from(GOOD_DIFF) == []


def test_a_changed_backend_shows_its_diff_not_the_no_op_note():
    rep = {"commands": [], "unchanged_files": ["/opt/x/package.json"],
           "changed_files": [{"path": "/opt/x/server.js", "diff": "+app.use('/api/a', require('./a'))"}],
           "roundtrip": {"ok": False, "get_status": 404, "get_body": "Cannot GET"}}
    why = rh.refusal_from(rep)[0]["why"]
    assert "EXACTLY as before" not in why
    assert "`/opt/x/server.js` changed: +app.use('/api/a'" in why


# ── §17.1411c — a PUT the backend cannot read ────────────────────────────────

NOJSON = json.loads((FIX / "report_put_without_json_parsing.json").read_text())


def test_a_backend_that_parses_no_json_body_is_named():
    """Live: CT 111's server.js never calls express.json(); every repair's PUT got
    req.body undefined and answered 400 -- and the evidence never said why."""
    assert NOJSON["roundtrip"]["get_status"] == 200 and NOJSON["roundtrip"]["put_status"] == 400
    assert NOJSON["parses_json_body"] is False
    why = rh.refusal_from(NOJSON)[0]["why"]
    assert "app.use(express.json())" in why and "`req.body` is undefined in every PUT" in why
    assert "the PUT carried exactly the body the GET returned" in why


def test_a_backend_that_parses_json_is_not_told_it_does_not():
    rep = {**NOJSON, "parses_json_body": True}
    assert "NO file of the backend parses a JSON body" not in rh.refusal_from(rep)[0]["why"]


def test_a_passing_round_trip_says_nothing_about_parsing():
    assert GOOD_DIFF.get("roundtrip", {}).get("ok") is True
    assert rh.refusal_from(GOOD_DIFF) == []


# ── §17.1411d — the evidence a repair is shown is current ────────────────────

def test_a_tie_goes_to_the_newer_attempt():
    """Live: five repairs tied at 3330 while the kept best carried pre-§17.1411c evidence;
    with `<`, none replaced it, so the express.json() note was never shown."""
    src = inspect.getsource(execution_agent._pause_for_decision)
    assert 'if _sc <= _best["score"]:' in src and 'if _sc < _best["score"]:' not in src


def test_a_resumed_best_is_rehearsed_again_before_it_is_shown():
    src = inspect.getsource(execution_agent._pause_for_decision)
    i_resume = src.index('_best.update({"frame": None, "runbook": _kept["runbook"]')
    i_fresh = src.index("_fresh = await _rehearse_or_nothing(_best[\"runbook\"]")
    i_loop = src.index("for _attempt in range(rehearsal.REPAIRS):")
    assert i_resume < i_fresh < i_loop
