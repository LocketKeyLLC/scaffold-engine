"""§17.1984 — an ambiguity the confirm feedback answers is a stated decision, never a "Decide with the operator" step.

Live (job 69fe61c4, 2026-11-21): the refined brief had five ambiguities and the operator answered all five in one line,
"Operator answers: (1) Access: … (2) Chart span: … (5) Proposals: …". Only a number at the START of a line was read,
so every one read as unanswered and the plan opened with ASK1–ASK5, each saying "the operator was asked this and could
not answer". The fifth question itself was cut mid-sentence ("(b) a summary you can click to"): the refiner's 2048-token
budget ran out and the repaired JSON kept the stump. Both briefs are the live ones (paper-trading lab; no home data).
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import re
from unittest.mock import AsyncMock, MagicMock, patch

from app.modules import prompt_assembly as pa
from app.modules import unanswered as ua

_FX = pathlib.Path(__file__).parent / "fixtures"
LIVE = json.loads((_FX / "inline_numbered_answers_brief_69fe61c4.json").read_text())
OWN = json.loads((_FX / "own_numbering_brief_6c401b85.json").read_text())


def _brief(feedback: str, amb=None) -> dict:
    return {"ambiguities": list(amb or LIVE["ambiguities"]), "user_feedback": feedback}


# --- the live shape -------------------------------------------------------------------------------------------------

def test_the_live_one_line_answers_resolve_all_five():
    assert len(LIVE["ambiguities"]) == 5 and "\n" not in LIVE["user_feedback"]
    assert ua.unresolved(LIVE) == []
    assert ua.decisions_for_tasks([{"id": "T1", "name": "Build the dashboard route", "type": "task"}], LIVE) == []


def test_each_live_answer_sits_beside_its_own_question():
    pairs = {LIVE["ambiguities"].index(p["question"]) + 1: p["answer"] for p in ua.answers(LIVE)}
    assert pairs[1].startswith("Access: the lab's existing API key")
    assert pairs[2].startswith("Chart span: buttons for 1 week")
    assert pairs[3].startswith("Refresh: only on reload")
    assert pairs[5].startswith("Proposals: a one-line summary") and pairs[5].endswith("sources.")


def test_the_planner_is_given_the_answers_as_decisions():
    block = pa._brief_essentials(LIVE)
    assert "already decided" in block and "A: Chart span: buttons for 1 week / 1 month / 3 months / all." in block


# --- correct misses -------------------------------------------------------------------------------------------------

def test_two_answers_of_five_leave_three_decision_steps():
    brief = _brief("Answers: (1) the existing lab key. (3) only on reload.")
    left = ua.unresolved(brief)
    assert left == [LIVE["ambiguities"][i] for i in (1, 3, 4)]
    asks = ua.decisions_for_tasks([], brief)
    assert len(asks) == 3 and all(a["name"].startswith("Decide with the operator:") for a in asks)


def test_feedback_about_something_else_answers_nothing():
    for fb in ("Looks good, go ahead. Make it dark-themed and fast.",
               "Please keep the tests green and change nothing else."):
        assert ua.unresolved(_brief(fb)) == LIVE["ambiguities"]
        assert len(ua.decisions_for_tasks([], _brief(fb))) == 5


def test_a_non_answer_by_inline_number_stays_open():
    brief = _brief("(1) unsure (2) buttons for 1 week / 1 month / all")
    assert ua.unresolved(brief) == [LIVE["ambiguities"][0]] + LIVE["ambiguities"][2:]


def test_an_inline_number_out_of_order_or_inside_a_word_is_prose():
    # "(2)" after "(4)" is an aside; "f(3)" is not a marker; a number past the brief's count answers nothing
    brief = _brief("(1) existing key (4) net beside gross, see note (2) above, f(3) aside (9) extra")
    got = {LIVE["ambiguities"].index(p["question"]) + 1 for p in ua.answers(brief)}
    assert got == {1, 4}


# --- restated, unnumbered -------------------------------------------------------------------------------------------

def test_the_live_answers_restated_without_numbers_resolve_too():
    stripped = re.sub(r"\(\d\)\s*", "", LIVE["user_feedback"])
    assert ua.unresolved(_brief(stripped)) == []


def test_a_restatement_that_fits_two_questions_answers_neither():
    amb = ["Should the chart show daily returns or weekly returns?", "Should the table show daily returns or weekly returns?"]
    assert ua.unresolved({"ambiguities": amb, "user_feedback": "Show daily returns and weekly returns side by side."}) == amb


# --- the operator's own numbering -----------------------------------------------------------------------------------

def test_an_own_numbering_is_paired_by_what_it_says_not_by_its_number():
    pairs = {OWN["ambiguities"].index(p["question"]) + 1: p["answer"] for p in ua.answers(OWN)}
    assert "approves each order individually" not in pairs.get(4, "")      # the 4th question is the allow-list
    assert "allow-list" in pairs[4]
    assert "operator view shows" in pairs[7] and "Form 4" in pairs[8]
    for n, ans in pairs.items():                                            # nothing sits beside a stranger
        assert len(ua._subject_words(ans) & ua._subject_words(OWN["ambiguities"][n - 1])) >= 3


def test_a_full_numbering_is_still_trusted_by_number():
    brief = {"ambiguities": ["Which GPU?", "Which OS?"], "user_feedback": "(1) the P40 (2) Debian 12"}
    assert [p["answer"] for p in ua.answers(brief)] == ["the P40", "Debian 12"]


# --- the cut brief --------------------------------------------------------------------------------------------------

def test_the_live_fifth_question_was_cut():
    assert LIVE["ambiguities"][4].endswith("(b) a summary you can click to")


def test_a_reply_stopped_at_the_budget_is_seen_as_cut():
    from app.modules.idea_refinement import draw_was_cut
    cut = MagicMock(raw={"done_reason": "length"}, tokens_completion=2048)
    whole = MagicMock(raw={"done_reason": "stop"}, tokens_completion=2048)
    counted = MagicMock(raw={}, tokens_completion=2048)
    assert draw_was_cut(cut, 2048) and not draw_was_cut(whole, 2048) and draw_was_cut(counted, 2048)
    assert not draw_was_cut(MagicMock(raw={}, tokens_completion=900), 2048)


def test_the_cut_tail_of_the_last_list_is_dropped():
    from app.modules.idea_refinement import drop_cut_tail
    brief = {"title": "t", "goals": ["g"], "ambiguities": LIVE["ambiguities"][:5]}
    drop_cut_tail(brief, "job")
    assert brief["ambiguities"] == LIVE["ambiguities"][:4] and brief["goals"] == ["g"]
    s = {"title": "t", "description": "stops mid"}
    drop_cut_tail(s, "job")
    assert s == {"title": "t", "description": "stops mid"}


def _resp(args, done):
    r = MagicMock(success=True, error=None, model="m", total_duration_ms=1, text="",
                  raw={"done_reason": done}, tokens_completion=10)
    call = MagicMock()
    call.arguments = args
    r.tool_calls = [call]
    return r


def _db():
    ins = MagicMock()
    ins.scalar_one.return_value = "job-1984"
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[ins] + [MagicMock()] * 8)
    db.commit = AsyncMock()
    return db


def _refine(draws):
    from app.modules import idea_refinement as ir
    with patch("app.modules.idea_refinement.model_router") as mr, \
            patch("app.modules.engine_source.job_targets_the_engine", AsyncMock(return_value=False)):
        mr.tool_call = AsyncMock(side_effect=draws)
        out = asyncio.new_event_loop().run_until_complete(ir.refine_idea("dashboard", _db()))
        return out, [c.kwargs["max_tokens"] for c in mr.tool_call.call_args_list]


def test_a_cut_brief_is_redrawn_with_room_and_the_whole_one_kept():
    from app.modules.idea_refinement import REFINE_MAX_TOKENS
    base = {"title": "D", "description": "d", "domain": "eng", "goals": ["g"], "complexity": "low"}
    cut = dict(base, ambiguities=LIVE["ambiguities"][:5])
    whole = dict(base, ambiguities=LIVE["ambiguities"][:4] + ["How much of each rationale? (a) one line; (b) expandable."])
    out, budgets = _refine([_resp(cut, "length"), _resp(whole, "stop")])
    assert REFINE_MAX_TOKENS > 2048 and budgets == [REFINE_MAX_TOKENS, 2 * REFINE_MAX_TOKENS]
    assert out["refined_brief"]["ambiguities"][-1].endswith("(b) expandable.")


def test_a_brief_still_cut_after_the_redraw_loses_only_the_stump():
    base = {"title": "D", "description": "d", "domain": "eng", "goals": ["g"], "complexity": "low"}
    cut = dict(base, ambiguities=LIVE["ambiguities"][:5])
    out, budgets = _refine([_resp(cut, "length"), _resp(dict(cut), "length")])
    assert len(budgets) == 2 and out["refined_brief"]["ambiguities"] == LIVE["ambiguities"][:4]


def test_a_whole_brief_is_drawn_once():
    base = {"title": "D", "description": "d", "domain": "eng", "goals": ["g"], "complexity": "low",
            "ambiguities": LIVE["ambiguities"][:4]}
    out, budgets = _refine([_resp(base, "stop")])
    assert len(budgets) == 1 and out["refined_brief"]["ambiguities"] == LIVE["ambiguities"][:4]
