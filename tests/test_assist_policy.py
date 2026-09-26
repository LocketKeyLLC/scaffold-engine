"""§17.855 (audit "policy migration") — the server-side deterministic policy.

Covers the ported phrase gates (pivot / help / how-to), the post-filter
precedence (shell-result → pivot → help), the fill-if-empty semantics, and a
DRIFT-PARITY check that pins the server regexes against the pipeline copy in
`_assist_handlers.py` (the two must stay identical — the server path is now
authoritative, the pipeline copy is the /decide-unavailable fallback).
"""
from __future__ import annotations

import json
import pathlib

import pytest

from app.modules import assist_policy as P

ROOT = pathlib.Path(__file__).resolve().parents[1]


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


def test_override_pivot_fires_from_ask():
    # §17.855 live-A/B fix — the /decide model routes a question-framed pivot to
    # `ask`; the pivot gate must still catch it (skip/question alone let it escape)
    d = _decision("ask")
    out = P.apply_deterministic_overrides(
        d, "can't we just clean the existing install instead of wiping it?")
    assert out["action"] == "note"
    assert out["override"] == "pivot"


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

def _compiled_patterns(path):
    """Every module-level ``NAME_RE = re.compile(<literal>, …)`` in a file, as
    {name: pattern}. Parsed from SOURCE with `ast`, deliberately: the pipeline
    module runs in another container and importing it here used to raise, which
    the old test turned into `pytest.skip` — a parity gate that reported green
    while pinning nothing. Reading the file cannot skip.
    """
    import ast
    out = {}
    for node in ast.walk(ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))):
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
            continue
        fn = node.value.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "compile" and node.value.args):
            continue
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id.endswith("_RE"):
                try:
                    out[t.id] = ast.literal_eval(node.value.args[0])
                except ValueError:      # a computed pattern — nothing to pin
                    pass
    return out


def test_regex_parity_with_pipeline_copy():
    """§17.1174 — the INTERSECTION, not a hand-listed six.

    The old version asserted six names and skipped on any import error. It
    therefore did not cover `_SHELL_ERROR_RE`, which had drifted: the pipeline
    carried two `error:`/`fatal:`/`panic:`-at-line-start alternations the engine
    did not, so a failed `git push` or a Go panic scored shell_error=False on
    the SERVER path — the authoritative one since §17.855 — and `_override`
    gate 1 routed it to **submit**, committing the step. Measured on 5 error
    shapes, the two copies disagreed on 4.

    Comparing the intersection means a regex added to both files in future is
    pinned the day it lands, with nobody remembering to add it here.
    """
    engine = _compiled_patterns(ROOT / "app" / "modules" / "assist_policy.py")
    pipeline = _compiled_patterns(ROOT / "pipelines" / "_vendor" / "_assist_handlers.py")
    shared = sorted(set(engine) & set(pipeline))
    assert len(shared) >= 8, f"expected the vendored gates to share ≥8 regexes, found {shared}"
    drifted = {n: (engine[n], pipeline[n]) for n in shared if engine[n] != pipeline[n]}
    assert not drifted, (
        "these regexes exist in BOTH assist_policy.py and _assist_handlers.py and "
        f"have diverged — the two routing paths will disagree: {sorted(drifted)}"
    )


@pytest.mark.parametrize("line,is_error", [
    ("error: no such target", True),
    ("fatal: not a git repository", True),
    ("Error response from daemon: pull access denied", True),
    ("panic: runtime error: index out of range", True),
    ("E: Unable to locate package foo", True),
    ("Cloning into 'repo'...", False),
    ("total 48", False),
    ("Active: active (running) since Mon", False),
])
def test_the_shell_error_gate_sees_the_line_shapes_that_used_to_commit_a_step(line, is_error):
    """§17.1174 — each of these was measured False on the engine side and True
    on the pipeline side. A False here means `_override` gate 1 takes the clean
    path and SUBMITS the step on a paste that shows a failure."""
    paste = "root@pve:~# make\n" + line
    assert bool(P._SHELL_ERROR_RE.search(paste)) is is_error, line
    assert P._compute_signals(paste, None)["shell_error"] is is_error, line


def test_an_error_paste_routes_to_fix_not_submit():
    """The consequence, end to end through the post-filter."""
    for line in ("fatal: not a git repository", "panic: runtime error", "error: no such target"):
        paste = "root@pve:~# make\n" + line
        d = P.apply_deterministic_overrides(
            {"action": "question", "signals": P._compute_signals(paste, None)}, paste)
        assert d["action"] == "fix", line
        assert d["override"] == "shell_error"



# ── §17.867 — whats-next orientation gate ─────────────────────────────────────


@pytest.mark.parametrize("msg", [
    "whats next??", "What's next?", "what is next", "what now", "now what?",
    "where are we", "where am I at?", "next steps?", "ok, what's next!",
    "so what do i do now", "what should we do next",
])
def test_whats_next_matches_orientation_phrases(msg):
    assert P.looks_like_whats_next(msg), msg


@pytest.mark.parametrize("msg", [
    "what's next after I configure the firewall?",   # longer question — real ask
    "what port does jellyfin use",                    # how-to
    "done",                                           # advance verb, not orientation
    "the next steps failed with an error",            # statement
    "",
])
def test_whats_next_ignores_non_orientation(msg):
    assert not P.looks_like_whats_next(msg), msg


def test_override_routes_whats_next_to_status():
    """§17.867 — the live incident: /decide confidently returned NOTE for
    'whats next??' and the question was recorded into the notes ledger."""
    d = {"action": "note", "confidence": "high", "signals": {}}
    out = P.apply_deterministic_overrides(d, "whats next??")
    assert out["action"] == "status"
    assert out["override"] == "whats_next"
    assert out["confidence"] == "high"


def test_override_leaves_status_alone():
    d = {"action": "status", "confidence": "medium", "signals": {}}
    out = P.apply_deterministic_overrides(d, "what now?")
    assert out is d  # unchanged — no double-stamp


def test_shell_paste_still_beats_whats_next():
    """Precedence: shell evidence wins even if the paste somehow matched."""
    d = {"action": "note", "confidence": "high",
         "signals": {"shell_paste": True, "shell_error": True}}
    out = P.apply_deterministic_overrides(d, "whats next??")
    assert out["action"] == "fix"


# ── §17.1174 — the only gate that did not normalize smart punctuation ────────

@pytest.mark.parametrize("msg", [
    "what's next?", "what’s next?",          # U+2019 is what a phone keyboard sends
    "what’s next", "so, what’s next!",
])
def test_whats_next_survives_a_curly_apostrophe(msg):
    """Every sibling gate calls `normalize_punct` first; this one did not, so
    §17.867's orientation override missed a large share of real messages and
    the turn fell through to whatever /decide said — the routing §17.867 exists
    because it got wrong."""
    assert P.looks_like_whats_next(msg), msg


def test_whats_next_still_ignores_a_longer_question():
    assert not P.looks_like_whats_next("what’s next after I configure the bridge?")


# ── §17.1174 — the corpus the comments described but nothing asserted ────────
#
# This file is a 1,214-line regex policy engine with ~36 named patterns, each
# justified by a dated live incident quoted in its comment. Those comments carry
# real measurements ("matched 3 of 83 messages", "fires on 34 of them") that were
# done by hand, once, and are not reproducible — so every gate's evidence rotted
# into prose the moment it shipped. The project's own rule is "measure a detector
# on the real corpus"; this is that corpus, made re-runnable.
#
# The messages are not invented: each is the live turn its own §-entry already
# quotes verbatim in assist_policy.py, so nothing here is new to a public repo.
# A replay over the operator's OWN assist_turns lives in
# scripts/replay_assist_policy.py — that one cannot be committed (their words,
# their topology) and is the local instrument.

_POLICY_CORPUS = json.loads(
    (ROOT / "tests" / "fixtures" / "assist_policy_corpus.json").read_text(encoding="utf-8"))

_GATES = {
    "pivot": P.looks_like_pivot, "howto": P.looks_like_howto_question,
    "help": P.looks_like_help_request, "blocked": P.looks_like_blocked,
    "claim": P.looks_like_completion_claim, "hedged": P.hedged_completion_report,
    "denial": P.looks_like_completion_denial, "advance": P.has_advancement_signal,
    "evidence": P.is_completion_evidence, "whats_next": P.looks_like_whats_next,
    "add_step": P.looks_like_add_step_request, "confirm": P.looks_like_confirmation,
    "decline": P.looks_like_decline, "uncertain": P.expresses_uncertainty,
    "recommend": P.wants_a_recommendation, "gui": P.looks_like_gui_question,
}


@pytest.mark.parametrize("case", _POLICY_CORPUS["cases"],
                         ids=[c["§"] for c in _POLICY_CORPUS["cases"]])
def test_each_documented_live_turn_still_routes_the_way_its_entry_says(case):
    fired = {g for g, fn in _GATES.items() if fn(case["msg"])}
    missing = set(case["fires"]) - fired
    wrong = set(case["not"]) & fired
    assert not missing, f"§{case['§']} expected {sorted(missing)} to fire on {case['msg']!r}"
    assert not wrong, f"§{case['§']} expected {sorted(wrong)} NOT to fire on {case['msg']!r}"


@pytest.mark.parametrize("case", _POLICY_CORPUS["cases"],
                         ids=[c["§"] for c in _POLICY_CORPUS["cases"]])
def test_the_gate_invariants_hold_on_every_corpus_message(case):
    """Properties, not examples — these outlive any individual phrasing, and
    they are what a replay over 251 real operator turns confirmed:

      claim ⊆ evidence ⊆ advance   — §17.915 is strictly narrower than §17.891,
                                     which is strictly narrower than nothing.
      claim ∩ hedged = ∅           — §17.1017 split one shape by certainty.
      claim ∩ denial = ∅           — §17.899 is the mirror of §17.890.
    """
    m = case["msg"]
    if P.looks_like_completion_claim(m):
        assert P.is_completion_evidence(m), m
        assert not P.hedged_completion_report(m), m
        assert not P.looks_like_completion_denial(m), m
    if P.is_completion_evidence(m):
        assert P.has_advancement_signal(m), m
