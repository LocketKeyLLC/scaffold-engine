"""§17.1605 — an operator's numbered answers to the brief's numbered questions are answers.

Live (job 0fb4ea7f, 2026-10-15): the operator answered all eight ambiguities as "1. … 8. …". Only `Q:`/`A:` pairs were
understood, so every question read as unanswered: the plan carried "Decide with the operator" steps for two questions
already answered (what the bigger models are for; which models), and the planner never saw "stick to 2 TB" beside the
question about ai-vm's size. The brief is the live one (addresses masked).
"""
from __future__ import annotations

import json
import pathlib

from app.modules import prompt_assembly as pa
from app.modules import unanswered as ua

BRIEF = json.loads((pathlib.Path(__file__).parent / "fixtures" / "server_move_brief_2026_10_15.json").read_text())


def test_the_live_numbered_answers_resolve_every_question():
    assert len(BRIEF["ambiguities"]) == 8
    assert ua.parse_feedback(BRIEF["user_feedback"]) == [], "the old reader saw nothing"
    pairs = ua.answers(BRIEF)
    assert [p["question"] for p in pairs] == BRIEF["ambiguities"]
    assert all(p["answered"] for p in pairs)
    assert ua.unresolved(BRIEF) == []


def test_each_answer_sits_beside_its_own_question():
    pairs = ua.answers(BRIEF)
    assert "2 TB" in pairs[4]["answer"] and "16 GB RAM" in pairs[4]["question"]
    assert "Hugging Face" in pairs[6]["answer"]


def test_no_decision_step_is_inserted_for_an_answered_question():
    assert ua.decisions_for_tasks([{"id": "T1", "name": "Decide engine placement", "type": "task"}], BRIEF) == []


def test_the_planner_is_given_the_answers_paired():
    block = pa._brief_essentials(BRIEF) if hasattr(pa, "_brief_essentials") else ""
    assert "Q: VM 110 ai-vm has only 16 GB RAM" in block and "stick to 2 TB" in block


def test_a_non_answer_by_number_stays_open_and_a_stray_number_is_ignored():
    brief = {"ambiguities": ["Which GPU?", "Which OS?"], "user_feedback": "1. unsure\n2. Debian 12\n9. extra"}
    assert ua.unresolved(brief) == ["Which GPU?"]
    assert [p["answer"] for p in ua.answers(brief)] == ["unsure", "Debian 12"]


def test_q_a_pairs_still_win():
    brief = {"ambiguities": ["Which GPU?"], "user_feedback": "Q: Which GPU?\nA: P40\n1. ignored"}
    assert ua.unresolved(brief) == [] and ua.answers(brief)[0]["answer"].startswith("P40")


def test_continuation_lines_belong_to_the_answer_above():
    brief = {"ambiguities": ["A?", "B?"], "user_feedback": "1. first line\n   and more\n2) second"}
    assert [p["answer"] for p in ua.answers(brief)] == ["first line and more", "second"]
