"""§17.511 — research summary prompt has an explicit anti-bleed clause.

The summary previously said only "summarize collected entries" with no
instruction to stay within them, and both runtime guards (faithfulness, CoVe)
default off — so a summary could bleed unrelated training-data content
(research.md's "kubernetes → Svelte tutorial" gotcha). This pins the always-on
prompt-level guard.
"""
from app.modules.research_agent import SUMMARY_SYSTEM_V1


def test_summary_prompt_forbids_outside_knowledge():
    s = SUMMARY_SYSTEM_V1.lower()
    assert "only the provided entries" in s
    # Must explicitly forbid adding outside/recalled facts.
    assert "do not add" in s
    assert "no outside" in s or "recalled knowledge" in s


def test_summary_prompt_still_asks_for_specifics():
    # The anti-bleed clause must not have removed the "include facts/numbers"
    # intent — just scoped it to the entries.
    s = SUMMARY_SYSTEM_V1.lower()
    assert "numbers" in s
    assert "under 500 words" in s
