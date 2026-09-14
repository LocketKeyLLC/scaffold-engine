"""§17.1068 — property tests over the phrase gates (hypothesis).

The gates in assist_policy are regexes over operator text. Hand-cased tests
pin the phrases we thought of; these pin the INVARIANTS: no gate raises on
any string, normalisation is idempotent, a gate's answer does not depend on
surrounding whitespace, and a shell paste never reads as a completion
claim. Runs in the ordinary suite (hypothesis is a dev dependency); the
`smoke` marker keeps it in the CI lane.
"""
from __future__ import annotations

import pytest
from hypothesis import given, settings as h_settings, strategies as st

from app.modules import assist_policy as P

TEXT = st.text(min_size=0, max_size=300)
GATES = [P.looks_like_pivot, P.looks_like_whats_next, P.looks_like_completion_claim,
         P.looks_like_add_step_request, P.looks_like_add_step_reply, P.is_bare_add_step_request,
         P.looks_like_confirmation, P.looks_like_decline, P.hedged_completion_report,
         P.expresses_uncertainty, P.looks_like_howto_question, P.looks_like_help_request]


@pytest.mark.smoke
@h_settings(max_examples=300, deadline=None)
@given(TEXT)
def test_no_gate_raises_and_all_return_bool(s):
    for g in GATES:
        assert isinstance(g(s), bool), g.__name__


@pytest.mark.smoke
@h_settings(max_examples=300, deadline=None)
@given(TEXT)
def test_normalize_punct_is_idempotent_and_length_preserving_modulo_ellipsis(s):
    once = P.normalize_punct(s)
    assert P.normalize_punct(once) == once
    assert len(once) >= len(s) - s.count("…") * 0  # never shorter (ellipsis expands)


@pytest.mark.smoke
@h_settings(max_examples=300, deadline=None)
@given(TEXT, st.sampled_from(["", " ", "\n", "  \t"]))
def test_leading_and_trailing_whitespace_do_not_change_a_gate(s, pad):
    for g in (P.looks_like_pivot, P.looks_like_completion_claim, P.looks_like_add_step_request, P.is_bare_add_step_request):
        assert g(s) == g(pad + s + pad), g.__name__


@pytest.mark.smoke
@h_settings(max_examples=200, deadline=None)
@given(st.from_regex(r"[a-z]{1,8}@[a-z]{1,8}:~[#$] [a-z ]{0,40}", fullmatch=True), TEXT)
def test_a_shell_prompt_line_is_never_a_bare_completion_claim(prompt_line, tail):
    msg = prompt_line + "\n" + tail
    sig = P._compute_signals(msg, None)
    assert sig["shell_paste"] is True
    assert not P.looks_like_completion_claim(msg)


@pytest.mark.smoke
@h_settings(max_examples=200, deadline=None)
@given(st.sampled_from(["question", "ask", "note", "status", "advance", "fix", "submit", "skip", "pause", "finalize", "add_step"]), TEXT)
def test_override_returns_a_decision_with_the_same_keys_plus_override_metadata(action, msg):
    d = {"action": action, "confidence": "high", "rationale": "", "signals": P._compute_signals(msg, None)}
    out = P.apply_deterministic_overrides(d, msg)
    assert set(d) <= set(out)
    assert out["action"] in ("question", "ask", "note", "status", "advance", "fix", "submit", "skip", "pause", "finalize", "add_step")
    if out is not d:
        assert out.get("override") or out.get("impact_normalized")
