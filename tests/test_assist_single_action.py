"""§17.1011 — one step is ONE action.

The live homelab job is the evidence: node T35 ("Configure reverse proxy")
emitted 4,817 chars over nine top-level sections, internally divided into
``Phase A`` (buy a domain, point DNS, forward two router ports), ``Phase B``
(discover an IP, rewrite a Caddyfile, reload) and ``Phase C`` (apply a firewall
group, then browse to it from a phone on cellular). Three phases, eight
numbered actions, four execution contexts — one "step" to finish before
advancing. Its 👉 headline (`cat` the Caddyfile) was not even the same work as
its ✅ close (public HTTPS loads from outside).
"""
import pytest

from app.config import settings
from app.modules import assist_guide

_BASE = "You are a co-pilot. Write the walkthrough."


def test_appends_directive_when_enabled():
    out = assist_guide.apply_single_action(
        _BASE, is_decision=False, enabled=True, max_steps=5)
    assert out.startswith(_BASE)
    assert "ONE STEP IS ONE ACTION" in out
    # 1 — the phase ban, naming the exact tokens T35 used.
    low = out.lower()
    for token in ("phase a", "part 2", "stage 1"):
        assert token in low, f"directive does not name {token!r}"
    # 3 — one place, naming the context mix that actually happened.
    assert "router" in low and "shell" in low
    # 4 — headline and finish line must describe the same work.
    assert "done when" in low and "do this next" in low


def test_noop_when_disabled():
    assert assist_guide.apply_single_action(
        _BASE, is_decision=False, enabled=False, max_steps=5) == _BASE


def test_noop_for_decision_nodes():
    # A decision's deliverable is a choice; GUIDE_SYSTEM_DECISION already holds
    # it to one at a time, and an "action" cap would misdescribe the task.
    assert assist_guide.apply_single_action(
        _BASE, is_decision=True, enabled=True, max_steps=5) == _BASE


@pytest.mark.parametrize("cap", [2, 5, 12])
def test_cap_is_interpolated_not_hardcoded(cap):
    out = assist_guide.apply_single_action(
        _BASE, is_decision=False, enabled=True, max_steps=cap)
    assert f"at most {cap} numbered actions" in out
    assert "{max_steps}" not in out, "format placeholder leaked to the model"


def test_composes_with_the_other_directives():
    s = assist_guide.apply_next_callout(_BASE, is_decision=False, enabled=True)
    s = assist_guide.apply_single_action(
        s, is_decision=False, enabled=True, max_steps=5)
    s = assist_guide.apply_done_criterion(s, is_decision=False, enabled=True)
    assert "Do this next" in s and "ONE STEP IS ONE ACTION" in s and "Done when" in s


def test_directive_reaches_both_guide_paths():
    """The stream path is the SPA path — the one the operator actually uses.

    §17.854/§17.975/§17.976/§17.984 were each a directive or grounding applied
    to one of the two guide paths and silently missing from the other. Both now
    call ``_build_guide_system``, so this asserts the SHARED builder carries the
    directive rather than counting call sites (a count passes just as happily
    when both copies are wrong).
    """
    src = (assist_guide.__file__)
    with open(src, encoding="utf-8") as fh:
        text = fh.read()
    assert text.count("system = _build_guide_system(") == 2, \
        "a guide path stopped using the shared directive builder"
    assert "apply_single_action(" in text

    class _Ctx:
        tool = "shell"
    prev = settings.assist_single_action_enabled
    try:
        settings.assist_single_action_enabled = True
        on = assist_guide._build_guide_system(_Ctx(), "normal", is_decision=False)
        settings.assist_single_action_enabled = False
        off = assist_guide._build_guide_system(_Ctx(), "normal", is_decision=False)
    finally:
        settings.assist_single_action_enabled = prev
    assert "ONE STEP IS ONE ACTION" in on
    assert "ONE STEP IS ONE ACTION" not in off


def test_valve_is_off_by_default():
    # House rule: behavioral prompt changes ship code-default-off, live via compose.
    from app.config import Settings
    assert Settings().assist_single_action_enabled is False
    assert Settings().assist_single_action_max_steps == 5
