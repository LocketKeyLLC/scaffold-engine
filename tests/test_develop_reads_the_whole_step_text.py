"""§17.1550 — the develop path reads the step's whole text (prompt_template-or-description).

Live, 5-section control-panel job c28ddeb0, 2026-10-14. T19 "Write Scaffold UI section" had its
entire spec AND an operator redraft correction in `prompt_template` (860 chars) while `description`
was empty (the planner wrote the step whole into prompt_template, and `decision_pause.add_review`
appended the review there because that is the column holding the text). But `develop.build_prompt`
and the three check-extractors read `description` alone, so the codegen model saw only the title
"Write Scaffold UI section" — it reinvented a generic HTTP-request builder every round and never
saw the correction. The regular executor already folds both columns in (§17.1237/ADD100); this is
the sibling that drifted. Reads now take `prompt_template or description`, matching
`plan_reconcile.STEP_TEXT_EXPR`.
"""
from __future__ import annotations

from app.modules import develop
from app.modules.develop import Host

HOST = Host(guest="111", vm=False, workdir="/opt/control-panel-backend",
            unit="control-panel.service", user="root", name="control-panel")

SPEC = "UI section for Scaffold engine: request submission form, job status display."
REVIEW = ("Operator review 2026-10-14 of the drafted block: NOT a generic HTTP request builder; "
          "post to the SAME-ORIGIN relative path /api/scaffold/request; no <DOMAIN>.")
CHECK = "`pct exec 111 -- curl -s http://127.0.0.1:3001/api/scaffold/jobs`"


def test_build_prompt_reads_prompt_template_when_description_empty():
    # the live shape: spec + review live in prompt_template, description is empty.
    node = {"node_key": "T19", "title": "Write Scaffold UI section",
            "description": "", "prompt_template": f"{SPEC}\n\n{REVIEW}"}
    prompt = develop.build_prompt(node, HOST, facts="", workspace={}, current=None,
                                  evidence="", round_no=1)
    assert SPEC in prompt
    assert "SAME-ORIGIN" in prompt            # the operator's correction reaches the model
    assert "generic HTTP request builder" in prompt


def test_build_prompt_still_reads_description_when_that_holds_the_text():
    # legacy shape: the planner wrote the step into description, prompt_template empty — unchanged.
    node = {"node_key": "T7", "title": "Write health route",
            "description": SPEC, "prompt_template": ""}
    prompt = develop.build_prompt(node, HOST, facts="", workspace={}, current=None,
                                  evidence="", round_no=1)
    assert SPEC in prompt


def test_prompt_template_wins_over_description_parity_with_step_text_expr():
    # STEP_TEXT_EXPR treats prompt_template as authoritative when non-empty.
    node = {"node_key": "T7", "title": "t",
            "description": "OLD stale text", "prompt_template": "NEW corrected text"}
    prompt = develop.build_prompt(node, HOST, facts="", workspace={}, current=None,
                                  evidence="", round_no=1)
    assert "NEW corrected text" in prompt
    assert "OLD stale text" not in prompt


def test_check_extractors_find_a_check_that_lives_in_prompt_template():
    # a step whose only backticked check sits in prompt_template (description empty).
    node = {"node_key": "T20", "title": "Add scaffold route",
            "description": "", "prompt_template": f"Add the route. Done when {CHECK} answers JSON."}
    inner = "pct exec 111 -- curl -s http://127.0.0.1:3001/api/scaffold/jobs"
    assert any(inner in c for c in develop.own_checks(node))
    assert any(inner in c for c in develop.done_checks(node, HOST))


def test_own_check_refusal_sees_a_prompt_template_check_for_a_non_codegen_step():
    # a non-CodeGen developed step that names its check in prompt_template is NOT refused for
    # "no own check" — before §17.1550 the empty description made it look checkless.
    node = {"node_key": "T20", "title": "Configure scaffold route", "tool": "Shell",
            "description": "", "prompt_template": f"Wire it. Done when {CHECK} answers JSON."}
    assert develop.own_check_refusal(node, HOST) == []
