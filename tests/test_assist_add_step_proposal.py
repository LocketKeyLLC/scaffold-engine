"""§17.1053 — "add a step for this" carries the proposal.

The engine's fix answers propose work under `## Needs its own step` and tell
the operator to reply **"add a step for this"**. Live, that reply reached
`add_step` as the whole request: the drafter never saw the proposal, so it
inserted a step titled "add a step for this" (ADD6, 2026-09-11); on 2026-09-13
a proposal naming three fixes told the operator to repeat the phrase three
times; and when /decide's thinking model starved (three 768-token draws, all
reasoning), the phrase was routed as a question. These pin all three ends:
the phrase routes deterministically, a bare request resolves the proposal
from the transcript and drafts one step per item, and a placeholder title can
never be inserted.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import model_router
from app.modules import assist_decide, assist_guide, assist_notes
from app.modules import assist_policy as P

# The live T37 fix answer's shape (2026-09-13 21:01, trimmed).
FIX_ANSWER = """## 👉 Do this next

**No command to run — this is a validation finding, not something to fix in this step.**

## Diagnosis

`nvidia-smi` failed with `Driver/library version mismatch` (NVML 580.178 vs loaded module 580.173.02).

## Then

So the validation step is **complete**, and it found three failures:

1. **GPU driver mismatch** in VM 110 — the loaded kernel module (580.173.02) does not match the NVML library (580.178).
2. **Control panel backend** in LXC 111 — `package.json` expects `index.js`, but only `server.js` exists, so nothing listens on port 3001.
3. **Caddy** in LXC 120 — the Caddyfile is incomplete (EOF error), so the reverse proxy cannot start.

## Needs its own step

Each of these three failures is a real problem that needs a dedicated fix step — they are not part of this validation-only step. Reply **"add a step for this"** for each one (or one at a time), and the engine will insert the fix steps before you move on to documentation.

## If that fails

If you want to double-check the mismatch yourself, run `cat /proc/driver/nvidia/version` inside VM 110.
"""

PROPOSAL_WITH_ITEMS = """## Diagnosis
Two foundations are missing.

## Needs its own step
- **Fix the NVIDIA driver mismatch in VM 110** — reinstall the matching driver and reboot.
- **Rebuild the Caddyfile in LXC 120** — write it whole, then `caddy validate`.

## Then
Come back here.
"""

THREE = [
    {"title": "Fix the NVIDIA driver/library mismatch in VM 110",
     "description": "Reinstall the 580.178 driver and reboot; done when nvidia-smi lists the Tesla P40."},
    {"title": "Start the control-panel backend on port 3001 in LXC 111",
     "description": "Point package.json at server.js; done when :3001 answers."},
    {"title": "Rebuild the Caddyfile in LXC 120 and start Caddy",
     "description": "Write the full Caddyfile; done when caddy.service is active."},
]


# ── policy: the phrase routes deterministically ──────────────────────────────

@pytest.mark.parametrize("msg", [
    "add a step for this", "Add a step for each one", "yes please add a step for this",
    "make it its own step", "a step for that", "add a step for this, one at a time",
    "add a step for each of them", "ok add another step for this",
])
def test_bare_add_step_requests(msg):
    assert P.looks_like_add_step_request(msg), msg
    assert P.is_bare_add_step_request(msg), msg


@pytest.mark.parametrize("msg", [
    "add a step to install the qemu guest agent in VM 110",
    "we need a step for the networking",
    "create a step to rebuild the caddyfile in LXC 120",
    "make it its own step: reinstall the nvidia driver",
])
def test_specific_add_step_requests_are_not_bare(msg):
    assert P.looks_like_add_step_request(msg), msg
    assert not P.is_bare_add_step_request(msg), msg


@pytest.mark.parametrize("msg", [
    "how do i do this", "next", "yes", "the install failed",
    "what does this step mean", "confirm", "add them",  # bare 'add them' needs the signal
])
def test_non_add_step_messages(msg):
    assert not P.looks_like_add_step_request(msg), msg


@pytest.mark.parametrize("msg", ["yes, add them", "add both", "insert it", "ok add the steps", "add all three"])
def test_add_step_reply_only_counts_after_a_proposal(msg):
    with_sig = {"action": "question", "confidence": "low", "rationale": "",
                "signals": {"last_assistant_proposed_step": True}}
    out = P.apply_deterministic_overrides(with_sig, msg)
    assert out["action"] == "add_step" and out["confidence"] == "high", msg
    assert out["override"] == "add_step_request"
    assert P.is_bare_add_step_request(msg), msg
    without = {"action": "question", "confidence": "low", "rationale": "", "signals": {}}
    assert P.apply_deterministic_overrides(without, msg)["action"] == "question", msg


def test_override_add_step_phrase_beats_a_starved_decision():
    # /decide fell back (low-confidence question); the engine's own phrase must still route.
    d = {"action": "question", "confidence": "low", "rationale": "fallback",
         "signals": {"shell_paste": False}}
    out = P.apply_deterministic_overrides(d, "add a step for this")
    assert out["action"] == "add_step" and out["confidence"] == "high"


def test_override_leaves_shell_evidence_and_operator_verbs_alone():
    paste = {"action": "question", "confidence": "high", "rationale": "",
             "signals": {"shell_paste": True, "shell_error": True}}
    out = P.apply_deterministic_overrides(paste, "root@pve:~# add a step\nNo such file or directory")
    assert out["action"] == "fix"  # gate 1 wins — a paste is evidence
    submit = {"action": "submit", "confidence": "high", "rationale": "", "signals": {}}
    assert P.apply_deterministic_overrides(submit, "done; add a step for the docs later")["action"] == "submit"


# ── decide: the proposed-step signal ─────────────────────────────────────────

def test_signal_sees_a_proposal_behind_the_nudge():
    hist = [{"role": "user", "content": "root@pve:~# nvidia-smi\nmismatch"},
            {"role": "assistant", "content": FIX_ANSWER},
            {"role": "assistant", "content": "↩︎ Or — reply `confirm` to mark it done."}]
    sig = assist_decide._compute_signals("add them", hist)
    assert sig["last_assistant_proposed_step"] is True
    assert sig["last_assistant_was_fix"] is False  # still the LAST assistant turn
    plain = [{"role": "assistant", "content": "## Fix\nrun this"}]
    assert assist_decide._compute_signals("x", plain)["last_assistant_proposed_step"] is False
    assert assist_decide._compute_signals("x", None)["last_assistant_proposed_step"] is False


# ── guide: section extraction + drafting ─────────────────────────────────────

def test_extract_needs_own_step_section():
    body = assist_guide.extract_needs_own_step(FIX_ANSWER)
    assert body and body.startswith("Each of these three failures")
    assert "If that fails" not in body and "## " not in body
    assert assist_guide.extract_needs_own_step("## Fix\nno proposal here") is None
    assert assist_guide.extract_needs_own_step("") is None


def test_proposal_excerpt_keeps_the_section_whole_when_bounded():
    long = ("## Diagnosis\n" + ("filler line about the system\n" * 300) + "\n"
            + "## Needs its own step\nRebuild the Caddyfile in LXC 120 — it is truncated.\n\n## Then\nback here")
    ex = assist_guide.proposal_excerpt(long, limit=2000)
    assert len(ex) < 2400
    assert "Rebuild the Caddyfile in LXC 120 — it is truncated." in ex
    assert ex.startswith("## Diagnosis")
    short = "## Needs its own step\nx"
    assert assist_guide.proposal_excerpt(short) == short


def _tool_resp(args):
    r = MagicMock()
    r.success = True
    r.text = ""
    call = MagicMock()
    call.arguments = args
    r.tool_calls = [call]
    return r


@pytest.mark.asyncio
async def test_draft_steps_bare_request_drafts_every_proposed_item_in_order():
    tc = AsyncMock(return_value=_tool_resp({"steps": THREE}))
    with patch.object(assist_guide.model_router, "tool_call", new=tc):
        out = await assist_guide.draft_steps(
            request="add a step for this", proposal=FIX_ANSWER,
            job_context="## Project goal\nHomelab on Proxmox")
    assert [s["title"] for s in out] == [s["title"] for s in THREE]
    sent = tc.await_args.kwargs["messages"][1]["content"]
    assert "ENGINE PROPOSAL" in sent and "Each of these three failures" in sent
    assert "bare reference" in sent  # the drafter is told to take the proposal as-is
    assert tc.await_args.kwargs["require_nonempty"] == "steps"


@pytest.mark.asyncio
async def test_draft_steps_bare_request_without_proposal_never_drafts():
    tc = AsyncMock()
    with patch.object(assist_guide.model_router, "tool_call", new=tc):
        out = await assist_guide.draft_steps(request="add a step for this", proposal=None)
    assert out == []
    tc.assert_not_awaited()


@pytest.mark.asyncio
async def test_draft_steps_drops_a_placeholder_title():
    # The ADD6 shape: the model echoes the phrase as the title.
    tc = AsyncMock(return_value=_tool_resp({"steps": [
        {"title": "add a step for this", "description": "add a step for this"},
        {"title": "Rebuild the Caddyfile in LXC 120", "description": "Write it whole."},
    ]}))
    with patch.object(assist_guide.model_router, "tool_call", new=tc):
        out = await assist_guide.draft_steps(request="add a step for this", proposal=FIX_ANSWER)
    assert [s["title"] for s in out] == ["Rebuild the Caddyfile in LXC 120"]


@pytest.mark.asyncio
async def test_draft_steps_model_down_falls_back_to_the_proposal_items():
    with patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(side_effect=RuntimeError("model down"))):
        out = await assist_guide.draft_steps(request="add them", proposal=PROPOSAL_WITH_ITEMS)
    assert [s["title"] for s in out] == [
        "Fix the NVIDIA driver mismatch in VM 110",
        "Rebuild the Caddyfile in LXC 120",
    ]
    assert all(len(s["description"]) > len(s["title"]) for s in out)


@pytest.mark.asyncio
async def test_draft_steps_model_down_bare_phrase_never_becomes_a_title():
    with patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(side_effect=RuntimeError("model down"))):
        out = await assist_guide.draft_steps(request="add a step for this", proposal=FIX_ANSWER)
    # The section itself has no items and its sentence is an instruction to the
    # operator ("…needs a dedicated fix step. Reply …") — never a title. The
    # bold-led findings the answer enumerated are the work.
    assert [s["title"] for s in out] == [
        "GPU driver mismatch in VM 110",
        "Control panel backend in LXC 111",
        "Caddy in LXC 120",
    ]
    assert all(not P.is_bare_add_step_request(s["title"]) for s in out)


def test_meta_sentence_never_becomes_a_title():
    only_meta = "## Needs its own step\nThis really needs a dedicated fix step. Reply \"add a step for this\".\n"
    assert assist_guide._steps_from_proposal_text(
        assist_guide.extract_needs_own_step(only_meta), only_meta) == []
    assert assist_guide._clean_drafted(
        {"steps": [{"title": "Reply 'add a step for this' and the engine will insert it",
                    "description": "x"}]}, request="add a step for this") == []


@pytest.mark.asyncio
async def test_draft_step_wrapper_accepts_the_single_step_shape():
    tc = AsyncMock(return_value=_tool_resp(
        {"title": "Configure the VM's network", "description": "Done when it pings 8.8.8.8."}))
    with patch.object(assist_guide.model_router, "tool_call", new=tc):
        out = await assist_guide.draft_step(request="set up the VM networking")
    assert out["title"] == "Configure the VM's network"


# ── notes.add_step: resolve from the transcript, chain N steps ───────────────

def _result(row):
    r = MagicMock()
    r.mappings.return_value.first.return_value = row
    return r


def _result_all(rows):
    r = MagicMock()
    r.mappings.return_value.all.return_value = rows
    return r


@pytest.mark.asyncio
async def test_add_step_bare_request_resolves_the_proposal_and_chains_three_steps():
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _result({"job_id": "j1", "status": "active", "current_node_key": "T37",
                 "metadata": {}, "notes": []}),                                  # session
        _result({"node_key": "T37", "depends_on": ["T36"],
                 "execution_order": 37, "tool": "shell", "domain": None}),         # anchor
        _result({"refined_brief": {"description": "Homelab"}}),                   # brief
        _result_all([{"content": "↩︎ Or — reply `confirm`."},                     # transcript
                     {"content": FIX_ANSWER}]),
        _result_all([{"node_key": "T36"}, {"node_key": "T37"}, {"node_key": "ADD1"}]),  # keys
        *[_result(None) for _ in range(6)],   # 3 × (INSERT node, INSERT step)
        *[_result(None) for _ in range(3)],   # 3 × UPDATE anchor depends_on
        _result(None), _result(None),         # reopen anchor (dag_nodes, assist_steps)
        _result(None),                        # session → first new key
    ])
    db.commit = AsyncMock()
    draft = AsyncMock(return_value=list(THREE))
    with patch("app.modules.assist_guide.draft_steps", new=draft):
        out = await assist_notes.add_step(session_id="s1", request="add a step for this", db=db)
    # the drafter saw the proposal, not just the phrase
    assert "Each of these three failures" in draft.await_args.kwargs["proposal"]
    assert draft.await_args.kwargs["request"] == "add a step for this"
    assert out["node_key"] == "ADD2" and out["count"] == 3
    assert [s["node_key"] for s in out["steps"]] == ["ADD2", "ADD3", "ADD4"]
    assert out["title"] == THREE[0]["title"]
    calls = db.execute.await_args_list
    inserts = [c for c in calls if "INSERT INTO dag_nodes" in str(c.args[0])]
    deps = [c.args[1]["deps"] for c in inserts]
    assert deps == [["T36"], ["T36", "ADD2"], ["T36", "ADD3"]]   # chained in order
    appended = [c.args[1]["nk"] for c in calls if "array_append" in str(c.args[0])]
    assert appended == ["ADD2", "ADD3", "ADD4"]                    # anchor waits on all
    repoint = [c for c in calls if "current_node_key = :nk" in str(c.args[0])]
    assert repoint and repoint[-1].args[1]["nk"] == "ADD2"          # session → FIRST
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_add_step_bare_request_with_no_proposal_refuses_a_placeholder():
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _result({"job_id": "j1", "status": "active", "current_node_key": "T37",
                 "metadata": {}, "notes": []}),
        _result({"node_key": "T37", "depends_on": [], "execution_order": 37,
                 "tool": "shell", "domain": None}),
        _result({"refined_brief": {}}),
        _result_all([{"content": "## Fix\nrun this"}, {"content": "## Diagnosis\nthat"}]),
    ])
    draft = AsyncMock()
    with patch("app.modules.assist_guide.draft_steps", new=draft), \
         pytest.raises(ValueError, match="nothing concrete to add"):
        await assist_notes.add_step(session_id="s1", request="add a step for this", db=db)
    draft.assert_not_awaited()
    assert not any("INSERT" in str(c.args[0]) for c in db.execute.await_args_list)


@pytest.mark.asyncio
async def test_add_step_empty_draft_refuses_to_insert():
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _result({"job_id": "j1", "status": "active", "current_node_key": "T37",
                 "metadata": {}, "notes": []}),
        _result({"node_key": "T37", "depends_on": [], "execution_order": 37,
                 "tool": "shell", "domain": None}),
        _result({"refined_brief": {}}),
    ])
    with patch("app.modules.assist_guide.draft_steps", new=AsyncMock(return_value=[])), \
         pytest.raises(ValueError, match="couldn't draft"):
        await assist_notes.add_step(session_id="s1", request="add a step to fix the thing", db=db)
    assert not any("INSERT" in str(c.args[0]) for c in db.execute.await_args_list)


# ── model_router: a starved tool-call draw switches thinking off ─────────────

def _starved():
    r = MagicMock()
    r.success = True
    r.text = ""
    r.tool_calls = []
    r.raw = {"done_reason": "length", "done": True,
             "message": {"role": "assistant", "content": "", "tool_calls": []}}
    return r


@pytest.mark.asyncio
async def test_tool_call_switches_think_off_after_a_starved_draw():
    good = _tool_resp({"action": "add_step"})
    once = AsyncMock(side_effect=[_starved(), good])
    with patch.object(model_router, "_tool_call_once", new=once):
        out = await model_router.tool_call(
            [{"role": "user", "content": "x"}], [MagicMock()], role="model_general")
    assert out is good
    assert once.await_args_list[0].kwargs["think"] is None
    assert once.await_args_list[1].kwargs["think"] is False


@pytest.mark.asyncio
async def test_tool_call_prose_empty_draw_keeps_thinking():
    # Not starved (stop, not length): the plain §17.583 redraw, thinking untouched.
    prose = MagicMock(); prose.success = True; prose.text = "I think…"; prose.tool_calls = []
    prose.raw = {"done_reason": "stop", "message": {"content": "I think…"}}
    good = _tool_resp({"action": "ask"})
    once = AsyncMock(side_effect=[prose, good])
    with patch.object(model_router, "_tool_call_once", new=once):
        await model_router.tool_call([{"role": "user", "content": "x"}], [MagicMock()], role="model_general")
    assert once.await_args_list[1].kwargs["think"] is None


def test_draw_starved_shape():
    assert model_router._draw_starved(_starved())
    assert not model_router._draw_starved(_tool_resp({"a": 1}))
    assert not model_router._draw_starved(None)


def test_think_opts_only_reach_ollama():
    ol = MagicMock(); ol.name = "ollama"
    oa = MagicMock(); oa.name = "openai"
    assert model_router._think_opts(ol, False) == {"think": False}
    assert model_router._think_opts(oa, False) == {}
    assert model_router._think_opts(ol, None) == {}


# ── §17.1053b: a reshape tag must not hijack add_step ────────────────────────

def test_override_normalizes_reshape_on_add_step():
    d = {"action": "add_step", "confidence": "high", "plan_impact": "reshape",
         "rationale": "", "signals": {}}
    out = P.apply_deterministic_overrides(d, "add a step for this")
    assert out["action"] == "add_step" and out["plan_impact"] == "none"
    assert out["impact_normalized"] == "add_step_is_the_plan_change"
    # a phrase-forced add_step over a reshape-tagged question is normalised too
    q = {"action": "question", "confidence": "high", "plan_impact": "reshape",
         "rationale": "", "signals": {}}
    out = P.apply_deterministic_overrides(q, "add a step for this")
    assert out["action"] == "add_step" and out["plan_impact"] == "none"
    # untouched otherwise
    n = {"action": "note", "confidence": "high", "plan_impact": "reshape", "rationale": "", "signals": {}}
    assert P.apply_deterministic_overrides(n, "use zfs instead of lvm")["plan_impact"] == "reshape"


# ── §17.1062 — mutation survivors in `_override` (make mutate baseline):
# each gate is asserted over EVERY action it names, and the actions it
# leaves alone are asserted untouched.

@pytest.mark.parametrize("action", ["question", "ask", "note", "status", "advance", "fix"])
def test_add_step_phrase_overrides_every_listed_action(action):
    d = {"action": action, "confidence": "high", "rationale": "", "signals": {}}
    out = P.apply_deterministic_overrides(d, "add a step for this")
    assert out["action"] == "add_step" and out["override"] == "add_step_request", action


@pytest.mark.parametrize("action", ["submit", "skip", "pause", "finalize", "add_step"])
def test_add_step_phrase_leaves_operator_verbs_alone(action):
    d = {"action": action, "confidence": "high", "rationale": "", "signals": {}}
    assert P.apply_deterministic_overrides(d, "add a step for this").get("override") is None, action


def test_clean_shell_paste_leaves_fix_alone_and_error_paste_forces_fix_from_submit():
    clean = {"action": "fix", "confidence": "high", "rationale": "",
             "signals": {"shell_paste": True, "shell_error": False, "last_assistant_was_fix": False}}
    assert P.apply_deterministic_overrides(clean, "root@pve:~# ls\nok").get("override") is None
    err = {"action": "submit", "confidence": "high", "rationale": "",
           "signals": {"shell_paste": True, "shell_error": True}}
    out = P.apply_deterministic_overrides(err, "root@pve:~# x\nNo such file or directory")
    assert out["action"] == "fix" and out["override"] == "shell_error" and out["error_text"].startswith("root@pve")


def test_override_with_no_message_is_a_no_op():
    d = {"action": "question", "confidence": "low", "rationale": "", "signals": {}}
    assert P.apply_deterministic_overrides(d, "") is d
    assert P.apply_deterministic_overrides(d, None) is d


# ── §17.1062 — `_compute_signals` survivors: the second fix marker, the
# three-turn lookback bound, and the early exit still returning the dict.

def test_second_fix_marker_and_exact_case():
    hist = [{"role": "assistant", "content": "something went wrong — let me help with that paste"}]
    assert assist_decide._compute_signals("x", hist)["last_assistant_was_fix"] is True
    hist_upper = [{"role": "assistant", "content": "SOMETHING WENT WRONG — LET ME HELP"}]
    assert assist_decide._compute_signals("x", hist_upper)["last_assistant_was_fix"] is False
    hist_tool = [{"role": "assistant", "content": "🔧 Troubleshooting\n## Fix"}]
    assert assist_decide._compute_signals("x", hist_tool)["last_assistant_was_fix"] is True


def test_proposal_lookback_is_exactly_three_assistant_turns():
    proposal = {"role": "assistant", "content": "## Needs its own step\nx"}
    filler = {"role": "assistant", "content": "ok"}
    user = {"role": "user", "content": "paste"}
    third_back = [proposal, user, filler, user, filler]           # proposal is the 3rd assistant turn back
    assert assist_decide._compute_signals("add them", third_back)["last_assistant_proposed_step"] is True
    fourth_back = [proposal, filler, user, filler, user, filler]  # 4th back → outside the window
    assert assist_decide._compute_signals("add them", fourth_back)["last_assistant_proposed_step"] is False


def test_signals_after_the_lookback_exit_still_return_the_full_dict():
    hist = [{"role": "assistant", "content": f"turn {i}"} for i in range(6)]
    out = assist_decide._compute_signals("root@pve:~# ls\nok", hist)
    assert set(out) == {"shell_paste", "shell_error", "last_assistant_was_fix", "last_assistant_proposed_step"}
    assert out["shell_paste"] is True and out["last_assistant_was_fix"] is False
