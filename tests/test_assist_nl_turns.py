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
        "assist_add_step_cmd": "ADDSTEP",
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


# ── §17.763: a help request is not a plan pivot ───────────────────────────


@pytest.mark.smoke
@pytest.mark.parametrize("msg", [
    "can you help me get the network bridge working here",
    "help me address the firewall rules on this step",
    "I need help with the storage config",
    "I'm stuck on connecting the VM to the internet",
    "walk me through configuring the VLANs",
    "I don't know how to set up the pool",
    "I need assistance addressing this networking issue",
    "not sure how to proceed with the interface",
])
def test_looks_like_help_request_positive(msg):
    assert _ah._looks_like_help_request(msg)


@pytest.mark.smoke
@pytest.mark.parametrize("msg", [
    "I picked ZFS with two VLANs",              # a decision, not a plan pivot
    "done, zero errors on the pool creation",   # a submit
    "what does the third bullet mean",          # clarify-this-step question
    "switch it all to Docker instead",          # a real pivot (caught upstream)
    "",
])
def test_looks_like_help_request_negative(msg):
    assert not _ah._looks_like_help_request(msg)


@pytest.mark.smoke
def test_help_request_routes_to_research_not_replan(pipe):
    # The reported bug: a request for help, classified as `question`, was run
    # through the §17.693 fuzzy reroute check and surfaced a spurious re-plan
    # ("🔀 …Apply these plan changes?"). It must route to hands-on research and
    # NEVER reach reroute_check. reroute_check is stubbed to a non-empty impact so
    # that, if it were reached, the output would be the re-plan surface — not ASK.
    with patch.object(_ah, "reroute_check",
                      MagicMock(return_value=[{"node_key": "T2", "action": "revise"}])) as rr:
        out, stubs, _ = _route(
            pipe, "can you help me get the network bridge working here",
            intent_dict={"intent": "question"},
        )
    assert "ASK" in out                          # routed to research/guidance
    rr.assert_not_called()                       # never reached the fuzzy reroute
    stubs["assist_research_cmd"].assert_called_once()
    stubs["assist_chat_turn"].assert_not_called()


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


# ── §17.705: pasted shell transcript → submit (deterministic, no classifier) ──


@pytest.mark.smoke
@pytest.mark.parametrize("msg", [
    "root@pve:~# qm list\n VMID NAME STATUS\n 100 vm1 running",
    "  root@pve:/etc/pve# cat storage.cfg\ndir: local\n    path /var/lib/vz",
    "root@pve:~$ zfs list\nNAME   USED  AVAIL\nrpool  2.1G  100G",
])
def test_looks_like_shell_evidence_positive(msg):
    assert _ah._looks_like_shell_evidence(msg)


@pytest.mark.smoke
@pytest.mark.parametrize("msg", [
    "how do I list the VMs?",
    "I think I'm on ubuntu 24.04",
    "what does root@pve mean?",                                   # no prompt line
    "root@pve:~# qm list\nVMID NAME\n... but which one do I remove?",  # ends on ?
    "",
])
def test_looks_like_shell_evidence_negative(msg):
    assert not _ah._looks_like_shell_evidence(msg)


@pytest.mark.smoke
def test_shell_paste_routes_to_submit_without_classifier(pipe):
    # The reported failure: a Proxmox audit paste was read as a question and
    # re-rendered, never recorded. It must deterministically submit — no LLM.
    paste = ("root@pve:~# pveum user list\nUSER            \nroot@pam\n"
             "root@pve:~# qm list\n VMID NAME STATUS")
    out, stubs, interp = _route(pipe, paste)
    assert "SUBMIT" in out
    interp.assert_not_called()                    # deterministic gate — no LLM round-trip
    args, kwargs = stubs["assist_submit"].call_args
    assert args[2] == "T1"                         # recalled current step
    assert args[3] == paste.strip()                # the whole transcript is the evidence


@pytest.mark.smoke
def test_last_assistant_was_fix_detection():
    # §17.748 — detect a Troubleshooting fix from the rendered marker / banner.
    assert _ah._last_assistant_was_fix(
        [{"role": "assistant", "content": "## 🔧 Troubleshooting `T14`\n\nRun this…"}])
    assert _ah._last_assistant_was_fix(
        [{"role": "assistant", "content": "_🔧 Sounds like something went wrong — let me help…_"}])
    # a normal guide walkthrough is NOT a fix; None/empty/user-only are safe
    assert not _ah._last_assistant_was_fix(
        [{"role": "assistant", "content": "## 🧭 How to do this step\napt update"}])
    assert not _ah._last_assistant_was_fix(None)
    assert not _ah._last_assistant_was_fix([{"role": "user", "content": "root@pve:~# ls"}])


def _route_with_history(pipe, msg, history, *, recall_nk="T14"):
    stubs = {"assist_submit": "SUBMIT", "assist_fix_cmd": "FIX", "assist_next": "ADVANCE"}
    patchers = {name: MagicMock(side_effect=lambda *a, _s=s, **k: iter([_s]))
                for name, s in stubs.items()}
    with patch.object(_ah, "assist_recall",
                      MagicMock(return_value={"last_node_key": recall_nk})), \
         patch.object(_ah, "assist_interpret", MagicMock(side_effect=AssertionError)), \
         patch.multiple(_ah, **patchers):
        out = "".join(_ah.assist_nl_turn(
            pipe, "s1", msg, node_key=recall_nk, chat_id="c1", history=history))
    return out, patchers


@pytest.mark.smoke
def test_shell_paste_midfix_continues_fix_not_submit(pipe):
    # §17.748 — the operator pasted output of a diagnostic the FIX asked them to
    # run; continue the fix (specific next action), don't auto-submit and fire the
    # "📝 Recording what you did… step not finished" verifier with generic advice.
    paste = ("root@pve:~# pvesm list local --content iso\n"
             "local:iso/ubuntu-26.04-live-server-amd64.iso iso 2918598656")
    history = [{"role": "assistant",
                "content": "## 🔧 Troubleshooting `T14`\n\nRun `pvesm list "
                           "local --content iso` and tell me what it shows."}]
    out, patchers = _route_with_history(pipe, paste, history)
    assert "FIX" in out and "SUBMIT" not in out
    args, _ = patchers["assist_fix_cmd"].call_args
    assert paste.strip() in args[2]   # the diagnostic output feeds the fix


@pytest.mark.smoke
def test_shell_paste_after_guide_still_submits(pipe):
    # Not mid-fix (last assistant was a guide) → §17.705 submit behavior holds.
    paste = "root@pve:~# systemctl is-active nginx\nactive"
    history = [{"role": "assistant",
                "content": "## 🧭 How to do this step\nInstall and start nginx."}]
    out, patchers = _route_with_history(pipe, paste, history, recall_nk="T5")
    assert "SUBMIT" in out and "FIX" not in out


@pytest.mark.smoke
def test_looks_like_shell_error():
    # §17.749 — high precision: real errors trip it, benign successes don't.
    assert _ah._looks_like_shell_error("scsi0: created\n-bash: scsi0: command not found")
    assert _ah._looks_like_shell_error("E: Unable to locate package foo")
    assert _ah._looks_like_shell_error("cp: cannot create file: Permission denied")
    assert _ah._looks_like_shell_error("could not resolve host: example.com")
    assert not _ah._looks_like_shell_error(
        "Logical volume created.\nVM 100 created successfully.")
    assert not _ah._looks_like_shell_error("VMID NAME STATUS\n100 vm running\n0 errors found")
    assert not _ah._looks_like_shell_error("")


@pytest.mark.smoke
def test_error_paste_after_guide_routes_to_fix(pipe):
    # §17.749 — a shell paste with a REAL error, even after a GUIDE (not a fix),
    # routes to fix (diagnose) instead of submit (which silently marked the step
    # done and advanced past a broken command — the reported failure).
    paste = ("root@pve:~# qm create 100 --boot order=ide2;scsi0\n"
             "scsi0: successfully created disk\n-bash: scsi0: command not found")
    history = [{"role": "assistant",
                "content": "## 🧭 How to do this step\nRun the qm create block."}]
    out, patchers = _route_with_history(pipe, paste, history)
    assert "FIX" in out and "SUBMIT" not in out
    args, _ = patchers["assist_fix_cmd"].call_args
    assert "command not found" in args[2]   # the error output feeds the fix


@pytest.mark.smoke
def test_checklist_request_routes_to_checklist_without_classifier(pipe):
    with patch.object(_ah, "assist_checklist_cmd",
                      side_effect=lambda *a, **k: iter(["CHECKLIST"])) as cl, \
         patch.object(_ah, "assist_interpret", MagicMock(side_effect=AssertionError)):
        out = "".join(_ah.assist_nl_turn(
            pipe, "s1", "what do you need from me?", node_key="T1", chat_id="c1"))
    assert "CHECKLIST" in out
    cl.assert_called_once()


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
    # §17.761 — total = 10, done = 3 → "3/10 steps done" (the bare "Step 4 of 10"
    # ordinal collided with the node-key style), and the progress leads the title
    assert "3/10 steps done" in out
    assert "6 to go" in out
    assert out.index("3/10 steps done") < out.index("Configure VLANs")


@pytest.mark.smoke
def test_render_step_last_step_and_no_counts():
    # last step: remaining == 0 → "last step"
    last = _ah.render_step({
        "node_key": "T9", "title": "Document it", "tool": "LLM", "depends_on": [],
        "base_prompt": "x", "upstream_outputs": {},
        "step_counts": {"committed": 4, "presented": 1},  # total 5, done 4 → last step
    })
    assert "4/5 steps done" in last  # §17.761 — was "Step 5 of 5"
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
@pytest.mark.parametrize("msg", [
    "am i supposed to use console? or one of the console selections?",
    "how do i run the installer from here?",
    "which option should i pick at the boot menu?",
    "what's the best way to get the OS installed?",
    "should i use the graphical or terminal installer?",
    "why won't the VM boot into the installer?",
])
def test_howto_question_reroutes_to_research(pipe, msg):
    # §17.733 — a how-to/should-I question the classifier filed as `question`
    # is upgraded to the research path (not a re-render of the current step).
    out, stubs, _ = _route(pipe, msg, intent_dict={"intent": "question"})
    assert "ASK" in out and "QUESTION" not in out
    assert stubs["assist_research_cmd"].call_args[0][2] == msg


@pytest.mark.smoke
@pytest.mark.parametrize("msg", [
    "what did you mean by that?",          # about the walkthrough, not how-to
    "can you make the port random?",       # a refinement
    "looks good to me",                    # a confirmation
])
def test_plain_question_still_re_renders(pipe, msg):
    # Non-how-to questions stay on the guide/refine path (no research upgrade).
    out, stubs, _ = _route(pipe, msg, intent_dict={"intent": "question"})
    assert "QUESTION" in out and "ASK" not in out


@pytest.mark.smoke
@pytest.mark.parametrize("msg", [
    "add a step for this",
    "can you add a step to set up the VM networking",
    "make it its own step",
    "we need a step for the networking",
    "create a step to configure the network properly",
])
def test_add_step_request_routes_to_add_step(pipe, msg):
    # §17.736 — explicit "make this a step" requests insert + guide a new step
    # (they never reach the classifier).
    out, stubs, interp = _route(pipe, msg)
    assert "ADDSTEP" in out
    interp.assert_not_called()   # deterministic pre-classifier intercept
    assert stubs["assist_add_step_cmd"].call_args[0][2] == msg


@pytest.mark.parametrize("msg", [
    "how do i set up the network",   # how-to → research, not add-step
    "the network isn't working",     # a problem report, not a step request
    "next step",                     # advancing
])
def test_non_add_step_messages_do_not_route_to_add_step(pipe, msg):
    assert not _ah._looks_like_add_step_request(msg)


def test_looks_like_add_step_gate():
    yes = ["add a step", "add another step for this", "make this its own step",
           "create a step for the networking", "we need a step for it",
           "a step for that", "needs its own step"]
    no = ["how do i do this", "the install failed", "use vault", "next",
          "what does this step mean", "should i reboot"]
    for m in yes:
        assert _ah._looks_like_add_step_request(m), m
    for m in no:
        assert not _ah._looks_like_add_step_request(m), m


def test_looks_like_howto_question_gate():
    yes = ["how do i attach the iso", "am i supposed to reboot now",
           "should i enable secure boot", "which kernel should i choose",
           "what should i do next here", "why is the console blank"]
    no = ["the install finished ok", "here is the output",
          "next", "use vault for the pool", "3 vlans please"]
    for m in yes:
        assert _ah._looks_like_howto_question(m), m
    for m in no:
        assert not _ah._looks_like_howto_question(m), m


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
