"""§17.756 — ground-or-ask: guide/fix emit a placeholder + a "Confirm these values"
section for any operator-specific value not in the confirmed facts, instead of
hardcoding a stale guess lifted from the transcript (the `ai-defruscio` leak).
"""
from app.modules import assist_guide

_BASE = "You are a co-pilot. Write the walkthrough."


def test_appends_directive_when_enabled():
    out = assist_guide.apply_ground_or_ask(_BASE, is_decision=False, enabled=True)
    assert out.startswith(_BASE)
    assert "GROUND OR ASK" in out
    assert "<SCREAMING_SNAKE_CASE>" in out
    assert "placeholder" in out.lower()
    # names the exact failure it prevents: lifting a stale value from the dialogue
    assert "abandoned" in out.lower() and "dialogue" in out.lower()


def test_noop_when_disabled():
    assert assist_guide.apply_ground_or_ask(_BASE, is_decision=False, enabled=False) == _BASE


def test_noop_for_decision_nodes():
    # A decision node's deliverable is a choice, not commands with values.
    assert assist_guide.apply_ground_or_ask(_BASE, is_decision=True, enabled=True) == _BASE


def test_composes_after_other_directives():
    # Order-independent: appended text is additive, base preserved.
    s = assist_guide.apply_next_callout(_BASE, is_decision=False, enabled=True)
    s = assist_guide.apply_ground_or_ask(s, is_decision=False, enabled=True)
    assert "Do this next" in s and "GROUND OR ASK" in s
