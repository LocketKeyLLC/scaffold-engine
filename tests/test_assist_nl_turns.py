"""§17.626 — natural-language assist turns (pipeline layer).

Plain chat in an active session drives the whole flow (advance / skip / submit /
fix / finalize / pause / question) without /assist subcommands, and a natural
sentence with no active session can START an assist session on a matching job.

Pins:
  * fast_classify_turn deterministically maps the obvious verbs; 'done' and
    substantive text fall through to the LLM classifier (None here);
  * assist_nl_turn routes each intent to the right handler; 'question' keeps the
    pre-§17.626 guide/refine turn;
  * match_assist_candidate: strong-unique match, ambiguous match, no-match;
  * render_step_footer teaches the natural report-back path.
"""
from unittest.mock import MagicMock, patch

import pytest

from tests._scaffold_router_setup import Pipeline, _mod

_ah = _mod._assist


@pytest.fixture
def pipe():
    return Pipeline()


# ── fast_classify_turn ────────────────────────────────────────────────────


@pytest.mark.smoke
@pytest.mark.parametrize("msg,intent", [
    ("next", "advance"),
    ("Next Step", "advance"),
    ("what's next", "advance"),
    ("move on", "advance"),
    ("skip", "skip"),
    ("skip this", "skip"),
    ("pause", "pause"),
    ("show me the result", "finalize"),
    ("wrap up", "finalize"),
])
def test_fast_classify_hits(msg, intent):
    assert _ah.fast_classify_turn(msg) == intent


@pytest.mark.smoke
@pytest.mark.parametrize("msg", [
    "done",                          # ambiguous → classifier decides
    "I picked ZFS with VLANs",       # substantive → classifier
    "how do I do this?",             # question → classifier
    "it failed with an error",       # fix → classifier
    "",
])
def test_fast_classify_misses_go_to_classifier(msg):
    assert _ah.fast_classify_turn(msg) is None


# ── assist_nl_turn routing ────────────────────────────────────────────────


def _route(pipe, msg, *, intent_dict=None, recall_nk="T1"):
    """Drive assist_nl_turn with the classifier + handlers stubbed; return the
    name of the handler that fired (via sentinel output)."""
    interp = MagicMock(return_value=intent_dict or {"intent": "question"})
    stubs = {
        "assist_next": "ADVANCE",
        "assist_skip": "SKIP",
        "assist_submit": "SUBMIT",
        "assist_fix_cmd": "FIX",
        "assist_done": "FINALIZE",
        "assist_simple_post": "PAUSE",
        "assist_chat_turn": "QUESTION",
    }
    patchers = {name: MagicMock(side_effect=lambda *a, _s=s, **k: iter([_s]))
                for name, s in stubs.items()}
    with patch.object(_ah, "assist_interpret", interp), \
         patch.object(_ah, "assist_recall", MagicMock(
             return_value={"last_node_key": recall_nk} if recall_nk else None)), \
         patch.multiple(_ah, **patchers):
        out = "".join(_ah.assist_nl_turn(pipe, "s1", msg, node_key=recall_nk, chat_id="c1"))
    return out, patchers, interp


@pytest.mark.smoke
def test_fast_verb_advances_without_classifier(pipe):
    out, stubs, interp = _route(pipe, "next")
    assert "ADVANCE" in out
    interp.assert_not_called()  # deterministic fast-path — no LLM round-trip


@pytest.mark.smoke
def test_submit_intent_records_evidence(pipe):
    out, stubs, _ = _route(
        pipe, "ok done — I chose ZFS",
        intent_dict={"intent": "submit", "evidence": "chose ZFS", "node_key": "T1"},
    )
    assert "SUBMIT" in out
    # evidence + node_key threaded into assist_submit(pipe, sid, node_key, evidence, …)
    args, kwargs = stubs["assist_submit"].call_args
    assert args[2] == "T1"
    assert args[3] == "chose ZFS"


@pytest.mark.smoke
def test_fix_intent_routes_to_fix(pipe):
    out, stubs, _ = _route(
        pipe, "it broke with permission denied",
        intent_dict={"intent": "fix", "error_text": "permission denied"},
    )
    assert "FIX" in out
    args, kwargs = stubs["assist_fix_cmd"].call_args
    assert args[2] == "permission denied"


@pytest.mark.smoke
def test_question_intent_keeps_guidance(pipe):
    out, stubs, _ = _route(
        pipe, "what does ZFS give me here?",
        intent_dict={"intent": "question"},
    )
    assert "QUESTION" in out
    stubs["assist_chat_turn"].assert_called_once()


@pytest.mark.smoke
def test_finalize_and_pause(pipe):
    out, _, _ = _route(pipe, "show me the result")  # fast-path finalize
    assert "FINALIZE" in out
    out2, _, _ = _route(pipe, "pause")               # fast-path pause
    assert "PAUSE" in out2


@pytest.mark.smoke
def test_submit_without_node_key_pulls_next(pipe):
    # No claimed step → don't dead-end; fetch the next step instead.
    out, stubs, _ = _route(
        pipe, "done", intent_dict={"intent": "submit"}, recall_nk=None,
    )
    assert "ADVANCE" in out
    stubs["assist_submit"].assert_not_called()


# ── match_assist_candidate ────────────────────────────────────────────────


_PROXMOX = {"job_id": "j1", "title": "Proxmox VE Installation and Configuration on Dual Xeon", "status": "assisted_running"}
_FIREWALL = {"job_id": "j2", "title": "Firewall and VPN Gateway Setup", "status": "awaiting_assist"}


@pytest.mark.smoke
def test_match_strong_unique():
    match, ambiguous = _ah.match_assist_candidate(
        "help me set up proxmox on the dual xeon box", [_PROXMOX, _FIREWALL],
    )
    assert match is _PROXMOX and ambiguous is False


@pytest.mark.smoke
def test_match_none_for_new_idea():
    # A genuinely new project must NOT hijack an existing job.
    match, ambiguous = _ah.match_assist_candidate(
        "build a rust markdown linter", [_PROXMOX, _FIREWALL],
    )
    assert match is None and ambiguous is False


@pytest.mark.smoke
def test_match_ambiguous_on_tie():
    a = {"job_id": "a", "title": "alpha beta", "status": "planning"}
    b = {"job_id": "b", "title": "alpha beta gamma", "status": "planning"}
    match, ambiguous = _ah.match_assist_candidate("do the alpha beta thing", [a, b])
    assert match is not None and ambiguous is True


@pytest.mark.smoke
def test_match_ambiguous_on_weak_single_token():
    match, ambiguous = _ah.match_assist_candidate("proxmox", [_PROXMOX, _FIREWALL])
    assert match is _PROXMOX and ambiguous is True  # only 1 shared token → offer list


# ── try_natural_start ─────────────────────────────────────────────────────


@pytest.mark.smoke
def test_try_natural_start_strong_starts_session(pipe):
    with patch.object(_ah, "fetch_assist_candidates",
                      MagicMock(return_value=[_PROXMOX, _FIREWALL])), \
         patch.object(_ah, "assist_start",
                      MagicMock(side_effect=lambda *a, **k: iter(["STARTED"]))) as start:
        gen = _ah.try_natural_start(pipe, "set up proxmox on the dual xeon box", "c1")
        out = "".join(gen)
    assert "STARTED" in out
    assert start.call_args[0][1] == "j1"  # started the matched job_id


@pytest.mark.smoke
def test_try_natural_start_ambiguous_lists(pipe):
    a = {"job_id": "a", "title": "alpha beta", "status": "planning"}
    b = {"job_id": "b", "title": "alpha beta gamma", "status": "planning"}
    with patch.object(_ah, "fetch_assist_candidates", MagicMock(return_value=[a, b])):
        gen = _ah.try_natural_start(pipe, "the alpha beta thing", "c1")
        out = "".join(gen)
    assert "which job" in out.lower()
    assert "/assist a" in out and "/assist b" in out


@pytest.mark.smoke
def test_try_natural_start_no_candidates_returns_none(pipe):
    with patch.object(_ah, "fetch_assist_candidates", MagicMock(return_value=[])):
        assert _ah.try_natural_start(pipe, "set up proxmox", "c1") is None


@pytest.mark.smoke
def test_try_natural_start_no_match_returns_none(pipe):
    with patch.object(_ah, "fetch_assist_candidates",
                      MagicMock(return_value=[_PROXMOX])):
        assert _ah.try_natural_start(pipe, "build a rust cli", "c1") is None


# ── render helpers ────────────────────────────────────────────────────────


@pytest.mark.smoke
def test_render_step_footer_natural_language():
    out = _ah.render_step_footer({"node_key": "T1"})
    assert "just tell me what happened" in out.lower()
    assert "skip" in out.lower()
    assert "/assist submit" in out  # muted alias still present


@pytest.mark.smoke
def test_render_step_leads_with_title_not_jargon():
    out = _ah.render_step({
        "node_key": "T1", "title": "Decide ZFS vs LVM", "tool": "LLM",
        "depends_on": [], "base_prompt": "decide it", "upstream_outputs": {},
    })
    # Title leads; the old jargon header (Tool | Domain | Depends on) is gone.
    assert out.index("Decide ZFS vs LVM") < out.index("`T1`")
    assert "Domain:" not in out
