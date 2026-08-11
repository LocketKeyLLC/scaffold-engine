"""§17.758 — screen-state grounding: guide/fix for an interactive surface whose
current screen isn't confirmed OPENS by asking what's on screen, instead of
assuming a screen and sending keystrokes to the wrong place.
"""
from app.modules import assist_guide

_BASE = "You are a co-pilot. Write the walkthrough."


def test_appends_directive_when_enabled():
    out = assist_guide.apply_screen_grounding(_BASE, is_decision=False, enabled=True)
    assert out.startswith(_BASE)
    assert "CONFIRM THE STARTING STATE" in out
    # names the interactive surfaces + the conditional-first-action behavior
    assert "installer" in out.lower() and "console" in out.lower()
    assert "on screen" in out.lower()
    # carves out the ordinary-shell case so it doesn't nag on every command step
    assert "shell" in out.lower()


def test_noop_when_disabled():
    assert assist_guide.apply_screen_grounding(_BASE, is_decision=False, enabled=False) == _BASE


def test_noop_for_decision_nodes():
    assert assist_guide.apply_screen_grounding(_BASE, is_decision=True, enabled=True) == _BASE


def test_composes_after_ground_or_ask_and_callout():
    s = assist_guide.apply_next_callout(_BASE, is_decision=False, enabled=True)
    s = assist_guide.apply_ground_or_ask(s, is_decision=False, enabled=True)
    s = assist_guide.apply_screen_grounding(s, is_decision=False, enabled=True)
    assert "Do this next" in s and "GROUND OR ASK" in s and "CONFIRM THE STARTING STATE" in s
