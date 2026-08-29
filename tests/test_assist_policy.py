"""§17.855 (audit "policy migration") — the server-side deterministic policy.

Covers the ported phrase gates (pivot / help / how-to), the post-filter
precedence (shell-result → pivot → help), the fill-if-empty semantics, and a
DRIFT-PARITY check that pins the server regexes against the pipeline copy in
`_assist_handlers.py` (the two must stay identical — the server path is now
authoritative, the pipeline copy is the /decide-unavailable fallback).
"""
from __future__ import annotations

import pytest

from app.modules import assist_policy as P


# ── the ported phrase gates ───────────────────────────────────────────────────

@pytest.mark.parametrize("msg", [
    "actually, let's switch to ZFS instead",
    "scratch that, start over",
    "forget the VLANs, one flat network",
    "can't I just wipe the old containers and start fresh?",
    "why not just do it over the network?",
    "isn't it easier to clean the existing install?",
    "do I even need the USB step?",
    "make it random throughout",
])
def test_pivot_positive(msg):
    assert P.looks_like_pivot(msg) is True


@pytest.mark.parametrize("msg", [
    "what does step 2 mean?",
    "ok that worked",
    "the port is 8443",
    "run apt update first",
])
def test_pivot_negative(msg):
    assert P.looks_like_pivot(msg) is False


def test_pivot_smart_apostrophe_normalized():
    # §17.692 — a curly apostrophe from a phone must still match
    assert P.looks_like_pivot("can’t we just wipe it") is True


def test_pivot_kind_global_vs_directional():
    assert P.pivot_kind("change the tone throughout") == "preference"
    assert P.pivot_kind("switch to ZFS instead") == "decision"


@pytest.mark.parametrize("msg", [
    "how do I configure the bridge?",
    "which option should I pick?",
    "what's the best way to split the VLANs?",
    "why won't the interface come up?",
])
def test_howto_positive(msg):
    assert P.looks_like_howto_question(msg) is True


@pytest.mark.parametrize("msg", [
    "help me get the bridge up",
    "I'm stuck on the network config",
    "can you walk me through this",
    "I need assistance with addressing the subnet",
])
def test_help_positive(msg):
    assert P.looks_like_help_request(msg) is True


# ── the post-filter ───────────────────────────────────────────────────────────

def _decision(action, signals=None, **kw):
    d = {
        "action": action, "evidence": "", "error_text": "", "query": "",
        "note_text": "", "note_kind": "note", "plan_impact": "none",
        "suggestion": None, "confidence": "medium", "rationale": "r",
        "node_key": "T3", "title": "t", "is_decision": False,
        "signals": signals or {}, "unavailable": False,
    }
    d.update(kw)
    return d


def test_override_shell_error_forces_fix():
    d = _decision("submit", {"shell_paste": True, "shell_error": True,
                             "last_assistant_was_fix": False})
    out = P.apply_deterministic_overrides(d, "root@pve:~# zpool\n-bash: zpool: command not found")
    assert out["action"] == "fix"
    assert out["override"] == "shell_error"
    assert out["confidence"] == "high"
    assert "command not found" in out["error_text"]


def test_override_midfix_paste_forces_fix():
    d = _decision("submit", {"shell_paste": True, "shell_error": False,
                             "last_assistant_was_fix": True})
    out = P.apply_deterministic_overrides(d, "root@pve:~# ip a\n1: lo")
    assert out["action"] == "fix"
    assert out["override"] == "shell_error"


def test_override_clean_shell_forces_submit():
    d = _decision("question", {"shell_paste": True, "shell_error": False,
                               "last_assistant_was_fix": False})
    out = P.apply_deterministic_overrides(d, "root@pve:~# ls\ntotal 0")
    assert out["action"] == "submit"
    assert out["override"] == "shell_result"
    assert out["evidence"].startswith("root@pve")


def test_override_clean_shell_leaves_submit_alone():
    d = _decision("submit", {"shell_paste": True, "shell_error": False,
                             "last_assistant_was_fix": False})
    out = P.apply_deterministic_overrides(d, "root@pve:~# ls")
    assert "override" not in out  # already submit → no change


def test_override_pivot_forces_note():
    d = _decision("question")
    out = P.apply_deterministic_overrides(d, "actually, switch to ZFS instead")
    assert out["action"] == "note"
    assert out["override"] == "pivot"
    assert out["note_kind"] == "decision"
    assert out["plan_impact"] == "surface"
    assert "ZFS" in out["note_text"]


def test_override_help_question_forces_ask():
    d = _decision("question")
    out = P.apply_deterministic_overrides(d, "help me get the bridge up")
    assert out["action"] == "ask"
    assert out["override"] == "help_howto"
    assert "bridge" in out["query"]


def test_override_noop_when_nothing_matches():
    d = _decision("submit", {"shell_paste": False})
    out = P.apply_deterministic_overrides(d, "the port is 8443")
    assert out is d  # returned unchanged, same object


def test_override_pivot_beats_help():
    # a message that states a pivot AND reads help-ish → pivot wins (re-plan)
    d = _decision("question")
    out = P.apply_deterministic_overrides(
        d, "I'm stuck — can't we just scrap the VLANs instead?")
    assert out["action"] == "note"


def test_override_fill_if_empty_preserves_llm_value():
    # the LLM extracted a clean error_text; the post-filter must not clobber it
    d = _decision("question", {"shell_paste": True, "shell_error": True},
                  error_text="zpool missing")
    out = P.apply_deterministic_overrides(d, "root@pve:~# zpool\n-bash: ... not found")
    assert out["action"] == "fix"
    assert out["error_text"] == "zpool missing"  # preserved, not overwritten


# ── drift parity with the pipeline copy ───────────────────────────────────────

def test_regex_parity_with_pipeline_copy():
    """The pipeline's `_assist_handlers` holds the fallback copy of these gates.
    The two MUST stay byte-identical or routing diverges by path. Skips if the
    vendored pipeline module can't import in this lane (it runs in its own
    container); the server copy is still fully covered above."""
    try:
        import importlib
        h = importlib.import_module("pipelines._vendor._assist_handlers")
    except Exception:  # pragma: no cover - pipeline deps absent in the core lane
        pytest.skip("pipelines._vendor._assist_handlers not importable in this lane")
    assert P._PIVOT_RE.pattern == h._PIVOT_RE.pattern
    assert P._GLOBAL_CHANGE_RE.pattern == h._GLOBAL_CHANGE_RE.pattern
    assert P._QUESTION_PIVOT_RE.pattern == h._QUESTION_PIVOT_RE.pattern
    assert P._HOWTO_QUESTION_RE.pattern == h._HOWTO_QUESTION_RE.pattern
    assert P._HELP_REQUEST_RE.pattern == h._HELP_REQUEST_RE.pattern
