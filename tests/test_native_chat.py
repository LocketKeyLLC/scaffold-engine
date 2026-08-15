"""Tests for the native chat dispatcher — NL routing + confirm-cards (§17.790)."""
from unittest.mock import AsyncMock, patch

import pytest

from app.native_chat import confirm_cards, dispatch, nl_commands, renderers


async def _drain(gen) -> str:
    return "".join([piece async for piece in gen])


# ── confirm_cards ─────────────────────────────────────────────────────────────
@pytest.mark.smoke
def test_confirm_card_encode_decode_roundtrip():
    card = confirm_cards.render_card(
        "jobs_delete", {"target_ref": "proxmox", "_job_id": "abc"}, "Delete it?"
    )
    assert "Delete it?" in card
    assert "NL_CONFIRM:" in card
    pending = confirm_cards.extract_pending([{"role": "assistant", "content": card}])
    assert pending == {"intent": "jobs_delete", "slots": {"target_ref": "proxmox", "_job_id": "abc"}}


@pytest.mark.smoke
def test_extract_pending_none_without_marker():
    assert confirm_cards.extract_pending([{"role": "assistant", "content": "just text"}]) is None
    assert confirm_cards.extract_pending([]) is None


@pytest.mark.smoke
def test_extract_pending_only_immediately_preceding_assistant():
    """A stale card earlier in the history must not re-arm — only the last
    assistant turn counts."""
    card = confirm_cards.render_card("jobs_delete", {"_job_id": "x"}, "Delete?")
    messages = [
        {"role": "assistant", "content": card},   # stale
        {"role": "user", "content": "no"},
        {"role": "assistant", "content": "Cancelled."},  # latest, no marker
        {"role": "user", "content": "yes"},
    ]
    assert confirm_cards.extract_pending(messages) is None


@pytest.mark.smoke
@pytest.mark.parametrize("text,aff,neg", [
    ("yes", True, False), ("Yes, do it", True, False), ("sure", True, False),
    ("go ahead", True, False), ("no", False, True), ("nope", False, True),
    ("cancel that", False, True), ("maybe later", False, False),
    ("what jobs are running", False, False),
])
def test_affirmative_negative(text, aff, neg):
    assert confirm_cards.is_affirmative(text) is aff
    assert confirm_cards.is_negative(text) is neg


@pytest.mark.smoke
def test_strip_marker():
    card = confirm_cards.render_card("x", {}, "Prompt text")
    assert confirm_cards.strip_marker(card) == "Prompt text"


# ── renderers ─────────────────────────────────────────────────────────────────
@pytest.mark.smoke
def test_render_status():
    out = renderers.status({
        "status_counts": {"running": 2, "completed": 5, "cancelled": 1},
        "total_jobs": 8,
        "recent_jobs": [{"id": "abcdef123456", "title": "Proxmox", "status": "running"}],
    })
    assert "2 running" in out and "Proxmox" in out and "abcdef12" in out
    assert "5 completed" not in out  # terminal states filtered from the active line


@pytest.mark.smoke
def test_render_jobs_list_and_empty():
    assert "none found" in renderers.jobs_list({"jobs": []})
    out = renderers.jobs_list({"jobs": [{"id": "1234567890", "title": "T", "status": "done", "node_count": 3}], "total": 1})
    assert "12345678" in out and "3 nodes" in out


@pytest.mark.smoke
def test_render_cost_and_delete_result():
    out = renderers.cost({"job_id": "abcdef1234", "total_cost_usd": 0.0, "call_count": 19,
                          "total_prompt_tokens": 100, "total_completion_tokens": 200,
                          "by_provider": [{"provider": "ollama", "model": "m", "calls": 3, "cost_usd": 0.0}]})
    assert "19 calls" in out and "ollama/m" in out
    assert "Deleted job `abc`" in renderers.delete_result("job", "abc", 204, None)
    assert "Could not delete" in renderers.delete_result("job", "abc", 404, {"detail": "not found"})


# ── job resolution ────────────────────────────────────────────────────────────
_JOBS = {"jobs": [
    {"id": "aaaa1111-2222-3333", "title": "Proxmox GPU passthrough", "status": "running"},
    {"id": "bbbb4444-5555-6666", "title": "Sorting algorithms", "status": "completed"},
]}


@pytest.mark.smoke
async def test_resolve_job_by_title_fragment():
    with patch("app.native_chat.engine_client.get_json", AsyncMock(return_value=(200, _JOBS))):
        job = await nl_commands._resolve_job("proxmox")
    assert job[0] == "aaaa1111-2222-3333"


@pytest.mark.smoke
async def test_resolve_job_empty_returns_most_recent():
    with patch("app.native_chat.engine_client.get_json", AsyncMock(return_value=(200, _JOBS))):
        job = await nl_commands._resolve_job("")
    assert job[0] == "aaaa1111-2222-3333"


@pytest.mark.smoke
async def test_resolve_job_no_match_returns_none():
    with patch("app.native_chat.engine_client.get_json", AsyncMock(return_value=(200, _JOBS))):
        assert await nl_commands._resolve_job("nonexistent-topic") is None


# ── classify + dispatch ───────────────────────────────────────────────────────
def _classify(**over):
    base = {"intent": "none", "confidence": "low", "query": "", "job_ref": "",
            "topic": "", "depth": "", "cron": "", "tz": "", "model_role": "",
            "model_name": "", "prompt": "", "new_name": "", "target_ref": "",
            "url": "", "repo": "", "node_key": ""}
    base.update(over)
    return base


@pytest.mark.smoke
async def test_dispatch_immediate_read_executes():
    status_body = {"status_counts": {"running": 1}, "total_jobs": 1, "recent_jobs": []}
    with patch("app.modules.command_guide.classify_command",
               AsyncMock(return_value=_classify(intent="status", confidence="high"))), \
         patch("app.native_chat.engine_client.get_json", AsyncMock(return_value=(200, status_body))):
        gen = await nl_commands.classify_and_dispatch("what's running")
        out = await _drain(gen)
    assert "Active jobs" in out and "1 running" in out


@pytest.mark.smoke
async def test_dispatch_unhandled_intent_falls_through():
    with patch("app.modules.command_guide.classify_command",
               AsyncMock(return_value=_classify(intent="model_set", confidence="high"))):
        assert await nl_commands.classify_and_dispatch("set the coder model") is None


@pytest.mark.smoke
async def test_dispatch_low_confidence_falls_through():
    with patch("app.modules.command_guide.classify_command",
               AsyncMock(return_value=_classify(intent="status", confidence="medium"))):
        assert await nl_commands.classify_and_dispatch("hmm") is None


@pytest.mark.smoke
async def test_dispatch_missing_required_slot_clarifies():
    with patch("app.modules.command_guide.classify_command",
               AsyncMock(return_value=_classify(intent="jobs_find", confidence="high", query=""))):
        gen = await nl_commands.classify_and_dispatch("find a job")
        out = await _drain(gen)
    assert "what to search for" in out


@pytest.mark.smoke
async def test_dispatch_confirm_intent_emits_card():
    with patch("app.modules.command_guide.classify_command",
               AsyncMock(return_value=_classify(intent="jobs_delete", confidence="high", target_ref="proxmox"))), \
         patch("app.native_chat.engine_client.get_json", AsyncMock(return_value=(200, _JOBS))):
        gen = await nl_commands.classify_and_dispatch("delete the proxmox job")
        out = await _drain(gen)
    assert "NL_CONFIRM:" in out and "Proxmox" in out and "yes" in out.lower()
    # the card must NOT have deleted anything (no request_json call)


@pytest.mark.smoke
async def test_commit_executes_delete():
    pending = {"intent": "jobs_delete", "slots": {"_job_id": "aaaa1111", "_job_title": "Proxmox"}}
    with patch("app.native_chat.engine_client.request_json", AsyncMock(return_value=(204, None))) as req:
        gen = nl_commands.commit(pending)
        out = await _drain(gen)
    req.assert_awaited_once()
    assert req.await_args.args[0] == "DELETE"
    assert "Deleted job `Proxmox`" in out


# ── dispatch.route (precedence) ───────────────────────────────────────────────
@pytest.mark.smoke
async def test_route_owui_task_short_circuits():
    msgs = [{"role": "user", "content": "### Task:\nGenerate a title\n### Chat History:\n..."}]
    assert await dispatch.route(msgs) is None


@pytest.mark.smoke
async def test_route_pending_affirmative_commits():
    card = confirm_cards.render_card("jobs_delete", {"_job_id": "z", "_job_title": "T"}, "Delete?")
    msgs = [{"role": "assistant", "content": card}, {"role": "user", "content": "yes"}]
    with patch("app.native_chat.engine_client.request_json", AsyncMock(return_value=(204, None))):
        gen = await dispatch.route(msgs)
        out = await _drain(gen)
    assert "Deleted job `T`" in out


@pytest.mark.smoke
async def test_route_pending_negative_cancels():
    card = confirm_cards.render_card("jobs_delete", {"_job_id": "z"}, "Delete?")
    msgs = [{"role": "assistant", "content": card}, {"role": "user", "content": "no"}]
    gen = await dispatch.route(msgs)
    out = await _drain(gen)
    assert "Cancelled" in out


@pytest.mark.smoke
async def test_route_plain_message_delegates_to_classifier():
    with patch("app.modules.command_guide.classify_command",
               AsyncMock(return_value=_classify(intent="help", confidence="high"))):
        gen = await dispatch.route([{"role": "user", "content": "what can you do"}])
        out = await _drain(gen)
    assert "drive the engine" in out


# ===========================================================================
# §17.791 (Phase 3a) — conversational triage + /go synthesis.
# ===========================================================================
from app.native_chat import triage  # noqa: E402
from app.providers.base import ModelResponse  # noqa: E402


@pytest.mark.smoke
def test_strip_think_closed_and_open():
    assert triage._strip_think("<think>reasoning</think>\nHello") == "Hello"
    assert triage._strip_think("<thinking>partial and truncated") == ""
    assert triage._strip_think("no tags here") == "no tags here"


@pytest.mark.smoke
def test_window_pins_user_turns():
    turns = [
        {"role": "user", "content": "u1 fact"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2 fact"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
        {"role": "assistant", "content": "a3"},
    ]
    out = triage._window(turns, 2)
    # last 2 turns kept + every earlier user turn pinned
    assert out[-2:] == turns[-2:]
    contents = [m["content"] for m in out]
    assert "u1 fact" in contents and "u2 fact" in contents
    assert "a1" not in contents  # earlier assistant block dropped


@pytest.mark.smoke
def test_turns_drops_system():
    turns = triage._turns([
        {"role": "system", "content": "client sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "yo"},
    ])
    assert [m["role"] for m in turns] == ["user", "assistant"]


@pytest.mark.smoke
async def test_run_triage_strips_think_and_yields():
    resp = ModelResponse(text="<think>plan</think>\n**Scope so far:**\nA CLI tool.", success=True)
    with patch("app.model_router.chat", AsyncMock(return_value=resp)):
        out = await _drain(triage.run_triage([{"role": "user", "content": "build a CLI"}]))
    assert "Scope so far" in out and "plan" not in out


@pytest.mark.smoke
async def test_run_triage_empty_nudges():
    resp = ModelResponse(text="<think>only thinking</think>", success=True)
    with patch("app.model_router.chat", AsyncMock(return_value=resp)):
        out = await _drain(triage.run_triage([{"role": "user", "content": "build a CLI"}]))
    assert "couldn't reach the planner" in out.lower()


@pytest.mark.smoke
async def test_synthesize_returns_brief():
    resp = ModelResponse(text="Build a CLI that converts screenshots to a searchable PDF.", success=True)
    with patch("app.model_router.chat", AsyncMock(return_value=resp)):
        text, fb = await triage.synthesize([{"role": "user", "content": "screenshots to pdf"}])
    assert fb is False and "Build a CLI" in text


@pytest.mark.smoke
async def test_synthesize_falls_back_to_user_messages():
    resp = ModelResponse(text="", success=False, error="down")
    with patch("app.model_router.chat", AsyncMock(return_value=resp)):
        text, fb = await triage.synthesize([
            {"role": "user", "content": "a screenshots tool"},
            {"role": "assistant", "content": "**Scope...**"},
            {"role": "user", "content": "on pop os"},
        ])
    assert fb is True and text == "a screenshots tool on pop os"


@pytest.mark.smoke
async def test_run_go_submits_and_renders_brief():
    ideate = {"job_id": "abcd1234-5678-90ab-cdef-000000000000",
              "status": "awaiting_confirmation",
              "refined_brief": "Build a searchable-PDF CLI on Pop!_OS.",
              "feasibility": {"feasible": True, "summary": "Straightforward with Tesseract."}}
    with patch("app.native_chat.triage.synthesize", AsyncMock(return_value=("Build X", False))), \
         patch("app.native_chat.engine_client.request_json", AsyncMock(return_value=(200, ideate))):
        out = await _drain(triage.run_go([{"role": "user", "content": "/go"}]))
    assert "Launch brief" in out and "searchable-PDF CLI" in out
    assert "Straightforward with Tesseract" in out and "/confirm abcd1234" in out


@pytest.mark.smoke
async def test_run_go_nothing_to_synthesize():
    with patch("app.native_chat.triage.synthesize", AsyncMock(return_value=("", False))):
        out = await _drain(triage.run_go([{"role": "user", "content": "/go"}]))
    assert "Nothing to synthesize" in out


@pytest.mark.smoke
async def test_route_go_delegates_to_run_go():
    async def _fake_go(_msgs):
        yield "GO_CALLED"
    with patch("app.native_chat.triage.run_go", _fake_go):
        gen = await dispatch.route([{"role": "user", "content": "/go build it"}])
        assert await _drain(gen) == "GO_CALLED"


@pytest.mark.smoke
async def test_route_plain_message_falls_through_to_triage():
    async def _fake_triage(_msgs):
        yield "TRIAGE_CALLED"
    with patch("app.modules.command_guide.classify_command",
               AsyncMock(return_value=_classify(intent="none", confidence="low"))), \
         patch("app.native_chat.triage.run_triage", _fake_triage):
        gen = await dispatch.route([{"role": "user", "content": "build me a thing"}])
        assert await _drain(gen) == "TRIAGE_CALLED"


@pytest.mark.smoke
async def test_run_go_renders_structured_brief_dict():
    """/ideate returns refined_brief as a structured dict — render prose, not repr."""
    ideate = {"job_id": "afa6c127-0000-0000-0000-000000000000",
              "status": "awaiting_confirmation",
              "refined_brief": {"title": "PNG-to-PDF CLI",
                                "description": "Build a Python CLI that OCRs screenshots into a searchable PDF.",
                                "goals": ["ocr", "pdf"]},
              "feasibility": {"feasible": True, "summary": "Doable in an evening."}}
    with patch("app.native_chat.triage.synthesize", AsyncMock(return_value=("x", False))), \
         patch("app.native_chat.engine_client.request_json", AsyncMock(return_value=(200, ideate))):
        out = await _drain(triage.run_go([{"role": "user", "content": "/go"}]))
    assert "**PNG-to-PDF CLI**" in out
    assert "OCRs screenshots into a searchable PDF" in out
    assert "'goals'" not in out and "{" not in out  # no raw dict repr
    assert "/confirm afa6c127" in out
