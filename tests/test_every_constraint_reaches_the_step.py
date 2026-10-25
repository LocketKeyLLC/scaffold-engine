"""§17.1820 — every constraint and inventory item of the brief reaches the prompt; none is dropped by its position.

Live (follow-up 5b4880c0, 2026-10-25): the operator's decision on rejected proposals was added mid-project as the
11th constraint; the step prompts showed the first 8 (each cut at 200 chars), so the newest decision -- the one the
failing steps needed -- never reached them.
"""
from __future__ import annotations

from app.modules import assist_agent, prompt_assembly

DECISION = ("A proposal the risk check rejects is still recorded: a proposal row with its real size and its signal "
            "(signal_id set, quantity > 0, as the schema requires), plus a risk_check row with passed = false and the "
            "reason -- never a quantity of 0 or a missing signal (operator decision, 2026-10-25)")
BRIEF = {"constraints": [f"constraint number {i}" for i in range(10)] + [DECISION],
         "inputs_available": [f"input {i}" for i in range(12)]}


def test_the_eleventh_constraint_reaches_a_step_whole():
    out = prompt_assembly._brief_essentials(BRIEF)
    assert DECISION in out                                         # live: cut by [:8], and by [:200] too
    assert "constraint number 0" in out and "input 11" in out


def test_the_assist_prompts_see_it_too():
    out = assist_agent._brief_essentials_block(BRIEF)
    assert DECISION in out and "input 11" in out


def test_over_the_budget_the_rest_is_counted_not_dropped_silently():
    out = prompt_assembly.brief_items([f"item {i} " + "x" * 90 for i in range(100)], budget=1000)
    assert out.count("\n- item ") >= 8 and out.endswith("more not shown (over the prompt budget)")
    assert "- … " in out and len(out) < 1200
    assert prompt_assembly.brief_items(["", " ", None]) == ""
