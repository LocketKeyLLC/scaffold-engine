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
import base64
import re
from unittest.mock import MagicMock, patch

import pytest

from tests._scaffold_router_setup import Pipeline, _make_response, _mod

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
        "assist_status": "STATUS",
        "assist_plan": "PLAN",
        "assist_handoff": "HANDOFF",
        "assist_research_cmd": "ASK",
        "assist_env_cmd": "ENV",
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
    # No step_counts (or all-zero) → first walkthrough → full footer.
    out = _ah.render_step_footer({"node_key": "T1"})
    assert "just tell me what happened" in out.lower()
    assert "skip" in out.lower()
    assert "/assist submit" in out  # muted alias still present


@pytest.mark.smoke
def test_footer_full_on_first_step_trimmed_after():
    """§17.647 — the full 89-word footer shows once (first walkthrough); once any
    step is completed it trims to a one-liner so later steps aren't padded."""
    full = _ah.render_step_footer({"node_key": "T1", "step_counts": {"pending": 5}})
    trimmed = _ah.render_step_footer(
        {"node_key": "T3", "step_counts": {"committed": 2, "pending": 3}})
    assert len(full.split()) > len(trimmed.split()) * 2  # materially shorter
    assert "/assist handoff" in full          # full lists every control
    assert "/assist handoff" not in trimmed   # trimmed drops the exhaustive list
    # trimmed still keeps the essentials in reach
    assert "skip" in trimmed.lower()
    assert "where am i" in trimmed.lower()


@pytest.mark.smoke
@pytest.mark.parametrize("done_status", ["committed", "skipped", "handed_off", "done"])
def test_footer_trims_on_any_completed_status(done_status):
    out = _ah.render_step_footer({"node_key": "T2", "step_counts": {done_status: 1}})
    assert "/assist handoff" not in out  # trimmed


@pytest.mark.smoke
def test_render_step_leads_with_title_not_jargon():
    out = _ah.render_step({
        "node_key": "T1", "title": "Decide ZFS vs LVM", "tool": "LLM",
        "depends_on": [], "base_prompt": "decide it", "upstream_outputs": {},
    })
    # Title leads; the old jargon header (Tool | Domain | Depends on) is gone.
    assert out.index("Decide ZFS vs LVM") < out.index("`T1`")
    assert "Domain:" not in out


@pytest.mark.smoke
def test_render_step_shows_progress_position():
    """§17.675 — a forward-looking 'Step X of Y' so a first-timer knows where
    they are. done statuses count as completed → position is done+1."""
    step_counts = {"committed": 2, "skipped": 1, "presented": 1, "pending": 6}
    out = _ah.render_step({
        "node_key": "T4", "title": "Configure VLANs", "tool": "Shell",
        "depends_on": [], "base_prompt": "do it", "upstream_outputs": {},
        "step_counts": step_counts,
    })
    # total = 10, done = 3 → "Step 4 of 10", and the position leads the title
    assert "Step 4 of 10" in out
    assert "6 to go" in out
    assert out.index("Step 4 of 10") < out.index("Configure VLANs")


@pytest.mark.smoke
def test_render_step_last_step_and_no_counts():
    # last step: remaining == 0 → "last step"
    last = _ah.render_step({
        "node_key": "T9", "title": "Document it", "tool": "LLM", "depends_on": [],
        "base_prompt": "x", "upstream_outputs": {},
        "step_counts": {"committed": 4, "presented": 1},  # total 5, done 4 → step 5 of 5
    })
    assert "Step 5 of 5" in last
    assert "last step" in last
    # no step_counts (older orchestrator) → no progress line, no crash
    none = _ah.render_step({
        "node_key": "T1", "title": "First", "tool": "LLM", "depends_on": [],
        "base_prompt": "x", "upstream_outputs": {},
    })
    assert "Step " not in none.split("###")[0]


# ── §17.627 — new intents route to engine components ──────────────────────


@pytest.mark.smoke
@pytest.mark.parametrize("msg,intent", [
    ("where am i", "status"),
    ("progress", "status"),
    ("show me the plan", "explain_plan"),
    ("all the steps", "explain_plan"),
    ("you do the rest", "handoff"),
    ("do it for me", "handoff"),
])
def test_fast_classify_new_verbs(msg, intent):
    assert _ah.fast_classify_turn(msg) == intent


@pytest.mark.smoke
def test_handoff_routes_to_executor(pipe):
    out, stubs, _ = _route(pipe, "you handle this one",
                           intent_dict={"intent": "handoff", "node_key": "T1"})
    assert "HANDOFF" in out
    args, _ = stubs["assist_handoff"].call_args
    assert args[2] == "T1" and args[3] == "single"


@pytest.mark.smoke
def test_handoff_all_remaining_mode(pipe):
    # "the rest" → all_remaining (fast-path handoff, mode read from the message).
    out, stubs, _ = _route(pipe, "you do the rest")
    assert "HANDOFF" in out
    assert stubs["assist_handoff"].call_args[0][3] == "all_remaining"


@pytest.mark.smoke
def test_ask_routes_to_research(pipe):
    out, stubs, _ = _route(
        pipe, "is ZFS safe on non-ECC RAM?",
        intent_dict={"intent": "ask", "query": "is ZFS safe without ECC RAM"},
    )
    assert "ASK" in out
    # the researched query (not the raw chat) is what gets looked up.
    assert stubs["assist_research_cmd"].call_args[0][2] == "is ZFS safe without ECC RAM"


@pytest.mark.smoke
def test_status_and_plan_route(pipe):
    assert "STATUS" in _route(pipe, "where am i")[0]
    assert "PLAN" in _route(pipe, "show me the plan")[0]


@pytest.mark.smoke
def test_set_env_parses_substitutions(pipe):
    out, stubs, _ = _route(
        pipe, "my host IP is HOST_IP=10.0.0.5 on Ubuntu 24.04",
        intent_dict={"intent": "set_env"},
    )
    assert "ENV" in out
    _, kwargs = stubs["assist_env_cmd"].call_args
    assert kwargs["substitutions"] == {"HOST_IP": "10.0.0.5"}
    assert "Ubuntu 24.04" in (kwargs["profile"] or "")


@pytest.mark.smoke
@pytest.mark.parametrize("msg,level", [
    ("explain more, I'm a beginner", "detailed"),
    ("just give me the commands", "terse"),
])
def test_set_verbosity_reads_level(pipe, msg, level):
    out, stubs, _ = _route(pipe, msg, intent_dict={"intent": "set_verbosity"})
    assert "ENV" in out
    assert stubs["assist_env_cmd"].call_args.kwargs["verbosity"] == level


# ── §17.627 — verbosity / handoff-mode helpers ────────────────────────────


@pytest.mark.smoke
def test_verbosity_helper():
    assert _ah._verbosity_from_message("please be more detailed") == "detailed"
    assert _ah._verbosity_from_message("too verbose, shorter") == "terse"
    assert _ah._verbosity_from_message("go on") == "normal"


@pytest.mark.smoke
@pytest.mark.parametrize("msg", [
    "walk me through this",
    "explain it step by step",
    "I'm a beginner, help me",
    "eli5 please",
])
def test_beginner_language_does_not_force_detailed(msg):
    """§17.643 — asking for help in beginner language must NOT bump verbosity to
    `detailed` (which adds WHY-rationale and length). A beginner wants a clear,
    concise how-to; these fall through to the beginner-clear `normal` default."""
    assert _ah._verbosity_from_message(msg) == "normal"


@pytest.mark.smoke
def test_handoff_mode_helper():
    assert _ah._handoff_mode_from_message("do the rest for me") == "all_remaining"
    assert _ah._handoff_mode_from_message("you do this one") == "single"


# ── §17.627 — pick-list follow-up (resolve_candidate_pick) ─────────────────


_PENDING = ["j1", "j2", "j3"]


@pytest.mark.smoke
@pytest.mark.parametrize("msg,expected", [
    ("1", "j1"),
    ("2", "j2"),
    ("number 3", "j3"),
    ("the second one", "j2"),
    ("first", "j1"),
    ("last", "j3"),
    ("9", None),           # out of range
    ("nonsense", None),    # no positional, no name match (no candidates fetched)
])
def test_resolve_candidate_pick_positional(pipe, msg, expected):
    with patch.object(_ah, "fetch_assist_candidates", MagicMock(return_value=[])):
        assert _ah.resolve_candidate_pick(pipe, msg, _PENDING) == expected


@pytest.mark.smoke
def test_resolve_candidate_pick_by_name(pipe):
    cands = [
        {"job_id": "j1", "title": "Proxmox VE Installation", "status": "planning"},
        {"job_id": "j2", "title": "Firewall Gateway", "status": "planning"},
    ]
    with patch.object(_ah, "fetch_assist_candidates", MagicMock(return_value=cands)):
        assert _ah.resolve_candidate_pick(pipe, "the proxmox one", ["j1", "j2"]) == "j1"


@pytest.mark.smoke
def test_candidate_list_embeds_hidden_marker():
    out = _ah.render_candidate_list([
        {"job_id": "j1", "title": "A", "status": "planning"},
        {"job_id": "j2", "title": "B", "status": "planning"},
    ])
    # §17.660 — reference-link definition (invisible in OWUI), NOT an HTML
    # comment (which OWUI renders as visible literal text). Payload base64url,
    # recoverable back to the ordered ids.
    assert "[apick]: ASSIST_PICK:" in out
    assert "<!--" not in out                      # no visible comment marker
    enc = re.search(r"ASSIST_PICK:([A-Za-z0-9_-]+)", out).group(1)
    enc += "=" * (-len(enc) % 4)
    assert base64.urlsafe_b64decode(enc.encode()).decode() == "j1,j2"


# ── §17.627 — assist_plan renders the DAG ─────────────────────────────────


@pytest.mark.smoke
def test_pipe_pick_list_followup_starts_job(pipe):
    # Prior turn showed a pick-list (hidden UUID marker); a bare "1" starts the
    # first job (checked BEFORE the <2-char noise guard would swallow it).
    j1 = "11111111-1111-1111-1111-111111111111"
    j2 = "22222222-2222-2222-2222-222222222222"
    history = [
        {"role": "user", "content": "help me with the homelab"},
        {"role": "assistant", "content": f"which job?\n<!--ASSIST_PICK:{j1},{j2}-->"},
        {"role": "user", "content": "1"},
    ]
    with patch.object(pipe, "_call_triage", return_value="TRIAGE") as triage, \
         patch.object(_ah, "assist_start",
                      MagicMock(side_effect=lambda *a, **k: iter(["STARTED"]))) as start:
        out = "".join(pipe.pipe("1", "model-id", history, {}))
    assert "STARTED" in out
    assert "TRIAGE" not in out           # the noise guard / triage never ran
    triage.assert_not_called()
    assert start.call_args[0][1] == j1


@pytest.mark.smoke
def test_assist_plan_renders_dag_with_current_marker(pipe):
    sess = {"job_id": "job-1", "current_node_key": "T2"}
    dag = {"nodes": [
        {"node_key": "T1", "title": "First", "status": "done"},
        {"node_key": "T2", "title": "Second", "status": "presented"},
        {"node_key": "T3", "title": "Third", "status": "pending"},
    ]}
    with patch.object(_mod._HTTP_SESSION, "get", side_effect=[
        _make_response(200, sess), _make_response(200, dag),
    ]):
        out = "".join(_ah.assist_plan(pipe, "s1"))
    assert "The plan" in out and "3 step" in out
    assert "✅ First" in out
    assert "👉" in out and "you're here" in out  # current step marked
    assert "1 done" in out
