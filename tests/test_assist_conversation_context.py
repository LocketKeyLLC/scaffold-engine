"""§17.687 — recent-conversation recall in Assist Mode.

The assist generation endpoints historically saw only committed-node output
(the §17.650 digest) + captured notes + environment, never the live
back-and-forth. So a program the engine SUGGESTED a turn ago, or any
not-yet-committed discussion, was forgotten on the next turn — a follow-up
("define that one", "yes, do it") had no antecedent. These tests cover the
render helper, its injection into the guide/fix/classify prompts, and the
settings gate in assist_agent.

The model is always mocked — never rely on a real LLM draw.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings as _settings
from app.modules import assist_agent, assist_guide
from app.modules.prompt_assembly import StepContext


def _ctx(tool: str = "shell") -> StepContext:
    return StepContext(
        node_key="D2",
        title="Choose the media server",
        tool=tool,
        domain=None,
        system_prompt="sys",
        base_prompt="Decide which media server to run on the box.",
        upstream_outputs={},
        upstream_truncated_keys=[],
        grounding="",
        grounding_kind=None,
        assembled_prompt="Decide which media server to run on the box.",
    )


def _resp(text: str, success: bool = True):
    r = MagicMock()
    r.success = success
    r.text = text
    r.model = "mock"
    r.error = None
    return r


# ── render_conversation_block ──────────────────────────────────────────────


def test_render_conversation_block_empty_is_blank():
    assert assist_guide.render_conversation_block(None) == ""
    assert assist_guide.render_conversation_block([]) == ""
    assert assist_guide.render_conversation_block([{"role": "user", "content": "  "}]) == ""
    # malformed items are skipped, not raised on
    assert assist_guide.render_conversation_block(["nope", 5, {}]) == ""


def test_render_conversation_block_roles_and_order():
    out = assist_guide.render_conversation_block(
        [
            {"role": "assistant", "content": "I'd lean Jellyfin — it's free."},
            {"role": "user", "content": "yes, tell me more about that one"},
        ]
    )
    assert "Recent conversation" in out
    assert "You (assistant): I'd lean Jellyfin" in out
    assert "Operator: yes, tell me more" in out
    # most-recent-last: the operator line comes after the assistant line
    assert out.index("I'd lean Jellyfin") < out.index("yes, tell me more")


def test_render_conversation_block_char_budget_drops_oldest():
    history = [
        {"role": "assistant", "content": "A" * 400},
        {"role": "user", "content": "B" * 400},
        {"role": "assistant", "content": "KEEP-THIS-NEWEST"},
    ]
    out = assist_guide.render_conversation_block(history, max_chars=200)
    # Newest kept, oldest dropped to fit the budget.
    assert "KEEP-THIS-NEWEST" in out
    assert "AAAA" not in out


def test_render_conversation_block_truncates_runaway_single_turn():
    out = assist_guide.render_conversation_block(
        [{"role": "assistant", "content": "Z" * 5000}], max_chars=1000
    )
    assert "…[truncated]" in out
    # content capped near max_chars; the fixed header adds a few hundred chars.
    assert len(out) < 1000 + 400


def test_render_conversation_block_zero_budget_is_blank():
    assert assist_guide.render_conversation_block(
        [{"role": "user", "content": "hi"}], max_chars=0
    ) == ""


# ── injection into the guidance / fix / classify prompts ───────────────────


@pytest.mark.asyncio
async def test_guidance_injects_conversation():
    captured = {}

    async def _capture_chat(messages, **kw):
        captured["user"] = messages[1]["content"]
        return _resp("walk")

    convo = assist_guide.render_conversation_block(
        [{"role": "assistant", "content": "I'd lean Jellyfin because it's FOSS."}]
    )
    with patch.object(assist_guide.model_router, "chat", new=_capture_chat):
        await assist_guide.generate_guidance(
            ctx=_ctx("shell"), research=False, node_key="D2",
            is_decision=True, conversation=convo,
        )
    assert "Recent conversation" in captured["user"]
    assert "Jellyfin" in captured["user"]


@pytest.mark.asyncio
async def test_fix_injects_conversation():
    captured = {}

    async def _capture_chat(messages, **kw):
        captured["user"] = messages[1]["content"]
        return _resp("fix")

    convo = assist_guide.render_conversation_block(
        [{"role": "assistant", "content": "Earlier I told you to use port 8096."}]
    )
    with patch.object(assist_guide.model_router, "chat", new=_capture_chat):
        await assist_guide.generate_fix(
            ctx=_ctx("shell"), error_text="connection refused",
            research=False, node_key="D2", conversation=convo,
        )
    assert "Recent conversation" in captured["user"]
    assert "port 8096" in captured["user"]


@pytest.mark.asyncio
async def test_classify_turn_threads_conversation():
    captured = {}

    async def _capture_tool_call(messages, tools, **kw):
        captured["user"] = messages[1]["content"]
        r = MagicMock()
        r.success = True
        call = MagicMock()
        call.arguments = {"intent": "ask", "query": "tell me about it"}
        r.tool_calls = [call]
        return r

    convo = assist_guide.render_conversation_block(
        [{"role": "assistant", "content": "I'd suggest Jellyfin."}]
    )
    with patch.object(assist_guide.model_router, "tool_call", new=_capture_tool_call):
        res = await assist_guide.classify_turn(
            message="yes, that one", title="t", task_prompt="p", tool="shell",
            conversation=convo,
        )
    assert res["intent"] == "ask"
    assert "Recent conversation" in captured["user"]
    assert "Jellyfin" in captured["user"]


# ── settings gate (assist_agent._conversation_block_for) ───────────────────


def _enable(monkeypatch, *, enabled=True, turns=6, max_chars=4000):
    monkeypatch.setattr(_settings, "assist_conversation_context_enabled", enabled)
    monkeypatch.setattr(_settings, "assist_conversation_context_turns", turns)
    monkeypatch.setattr(_settings, "assist_conversation_context_max_chars", max_chars)


def test_conversation_block_gate_enabled(monkeypatch):
    _enable(monkeypatch)
    hist = [{"role": "assistant", "content": "I'd lean Jellyfin."}]
    assert "Jellyfin" in assist_agent._conversation_block_for(hist)


def test_conversation_block_gate_disabled(monkeypatch):
    hist = [{"role": "assistant", "content": "I'd lean Jellyfin."}]
    _enable(monkeypatch, enabled=False)
    assert assist_agent._conversation_block_for(hist) == ""
    _enable(monkeypatch, turns=0)
    assert assist_agent._conversation_block_for(hist) == ""
    _enable(monkeypatch, max_chars=0)
    assert assist_agent._conversation_block_for(hist) == ""


def test_conversation_block_gate_empty_history(monkeypatch):
    _enable(monkeypatch)
    assert assist_agent._conversation_block_for(None) == ""
    assert assist_agent._conversation_block_for([]) == ""


def test_conversation_block_windows_to_turns(monkeypatch):
    _enable(monkeypatch, turns=2)
    hist = [
        {"role": "user", "content": "OLDEST-DROP-ME"},
        {"role": "assistant", "content": "middle"},
        {"role": "user", "content": "newest"},
    ]
    out = assist_agent._conversation_block_for(hist)
    assert "OLDEST-DROP-ME" not in out
    assert "newest" in out
