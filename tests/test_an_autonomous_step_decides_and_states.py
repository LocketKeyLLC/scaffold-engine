r"""§17.1564 — an autonomous step DECIDES AND STATES an undecided brief question; only a guide asks it.

Live (job 263e920a, T8 "Produce missing metadata report", the final deliverable): the brief carried an
unanswered ambiguity — "If some items still lack a title, overview, or poster after the refresh, what should
happen? (a) list them (b) second refresh (c) leave them" — that matched the report step, so §17.1221's
ASK BEFORE YOU BUILD made the step open with that question. Nobody answers inside an autonomous run: the
verifier refused the question as "no report", the retry re-drew the same question, three retries, four
rounds, and the job blocked on a deliverable whose inputs were all in hand.
"""
from __future__ import annotations

import inspect

from app.modules import prompt_assembly as pa
from app.modules.unanswered import ask_first_block, decide_and_state_block

BRIEF = {
    "description": "Refresh Jellyfin metadata and report what still lacks it.",
    "ambiguities": ["If some items still lack a title, overview, or poster after the refresh, what should happen? "
                    "Options: (a) just list them in the final report and stop, (b) try a second refresh using "
                    "different metadata providers before reporting, or (c) leave them untouched."],
}
T8 = {"node_key": "T8", "title": "Produce missing metadata report", "tool": "LLM",
      "prompt_template": "Format the verification results into a human-readable report of items lacking a title, "
                         "overview or poster. This is the final deliverable."}


def test_the_question_reaches_the_step_both_ways_but_only_the_guide_asks():
    guide = pa.build_base_prompt(T8, BRIEF)
    run = pa.build_base_prompt(T8, BRIEF, autonomous=True)
    assert "ASK BEFORE YOU BUILD" in guide and "DECIDE AND STATE" not in guide
    assert "DECIDE AND STATE" in run and "ASK BEFORE YOU BUILD" not in run
    assert "what should happen" in run                 # the question itself is still shown — as a thing to decide
    assert "do NOT ask" in run and "Assumed:" in run and "CONSERVATIVE" in run


def test_no_open_question_means_neither_block():
    answered = {**BRIEF, "ambiguities": []}
    out = pa.build_base_prompt(T8, answered, autonomous=True)
    assert "DECIDE AND STATE" not in out and "ASK BEFORE YOU BUILD" not in out
    assert decide_and_state_block([]) == "" and ask_first_block([]) == ""


def test_the_executor_and_the_runbook_drafter_build_autonomously_and_the_guide_does_not():
    from app.modules import execution_agent as ea, supervised_runs as sr
    assert "build_base_prompt(node, brief, environment, autonomous=True)" in inspect.getsource(ea._build_prompt) \
        or "autonomous=True" in inspect.getsource(ea._build_prompt)
    assert "autonomous=True" in inspect.getsource(sr.draft_runbook)
    guide_src = inspect.getsource(pa)
    i = guide_src.index("base_prompt = build_base_prompt(node, brief, _env)")
    assert "autonomous" not in guide_src[i:i + 60]      # the assist guide keeps asking
