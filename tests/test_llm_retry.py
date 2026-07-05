"""§17.464 — tests for the shared retry-on-empty LLM guard."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.utils.llm_retry import (
    chat_until_nonempty,
    generate_until_nonempty,
    tool_call_until_args,
)

pytestmark = pytest.mark.asyncio


def _resp(text, *, success=True):
    return SimpleNamespace(text=text, success=success, error=None, model="m")


async def _call(mock):
    # generate is dependency-injected — pass the mock directly.
    return await generate_until_nonempty(
        mock, "p", {"role": "model_general"},
        system="s", temperature=0.3, max_tokens=8192, label="t",
    )


@pytest.mark.smoke
async def test_valid_first_draw_no_retry():
    mock = AsyncMock(side_effect=[_resp('{"ok": true}')])
    resp = await _call(mock)
    assert resp.text == '{"ok": true}'
    assert mock.call_count == 1


@pytest.mark.smoke
async def test_empty_then_valid_redraws_and_returns_valid():
    """The reported failure mode: empty draw 1, valid draw 2 → recovered."""
    mock = AsyncMock(side_effect=[_resp(""), _resp("real content")])
    resp = await _call(mock)
    assert resp.text == "real content"
    assert mock.call_count == 2


@pytest.mark.smoke
async def test_whitespace_only_counts_as_empty():
    mock = AsyncMock(side_effect=[_resp("  \n\t "), _resp("real")])
    resp = await _call(mock)
    assert resp.text == "real"
    assert mock.call_count == 2


@pytest.mark.smoke
async def test_all_empty_exhausts_and_returns_last():
    """All draws empty → returns the last (empty) resp so the caller's existing
    empty-handling (fail_job / fallback) still applies."""
    mock = AsyncMock(side_effect=[_resp(""), _resp(""), _resp("")])
    resp = await _call(mock)
    assert (resp.text or "").strip() == ""
    assert mock.call_count == 3  # draws=3


@pytest.mark.smoke
async def test_hard_failure_returns_immediately_no_retry():
    """success=False is a hard error — surface it at once, don't burn re-draws."""
    mock = AsyncMock(side_effect=[_resp("", success=False)])
    resp = await _call(mock)
    assert resp.success is False
    assert mock.call_count == 1


# ---------------------------------------------------------------------------
# §17.465 — chat_until_nonempty (messages-shaped sibling)
# ---------------------------------------------------------------------------

_MESSAGES = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]


async def _chat_call(mock, *, draws=3):
    # chat is dependency-injected — pass the mock directly.
    return await chat_until_nonempty(
        mock, _MESSAGES, {"role": "model_coder", "overrides": None},
        temperature=0.7, max_tokens=8192, draws=draws, label="node-exec T3",
    )


@pytest.mark.smoke
async def test_chat_valid_first_draw_no_retry():
    mock = AsyncMock(side_effect=[_resp("real content")])
    resp = await _chat_call(mock)
    assert resp.text == "real content"
    assert mock.call_count == 1


@pytest.mark.smoke
async def test_chat_empty_then_valid_redraws():
    """The job-4e3b8f01 failure mode: empty draw 1, valid draw 2 → recovered."""
    mock = AsyncMock(side_effect=[_resp(""), _resp("real content")])
    resp = await _chat_call(mock)
    assert resp.text == "real content"
    assert mock.call_count == 2


@pytest.mark.smoke
async def test_chat_whitespace_only_counts_as_empty():
    mock = AsyncMock(side_effect=[_resp("  \n\t "), _resp("real")])
    resp = await _chat_call(mock)
    assert resp.text == "real"
    assert mock.call_count == 2


@pytest.mark.smoke
async def test_chat_all_empty_exhausts_and_returns_last():
    mock = AsyncMock(side_effect=[_resp(""), _resp(""), _resp("")])
    resp = await _chat_call(mock)
    assert (resp.text or "").strip() == ""
    assert mock.call_count == 3


@pytest.mark.smoke
async def test_chat_hard_failure_returns_immediately():
    mock = AsyncMock(side_effect=[_resp("", success=False)])
    resp = await _chat_call(mock)
    assert resp.success is False
    assert mock.call_count == 1


@pytest.mark.smoke
async def test_chat_forwards_messages_and_budget():
    """Routing kwargs + messages + the generous budget reach chat verbatim."""
    mock = AsyncMock(side_effect=[_resp("ok")])
    await _chat_call(mock)
    _, kwargs = mock.call_args
    assert kwargs["messages"] is _MESSAGES
    assert kwargs["max_tokens"] == 8192
    assert kwargs["temperature"] == 0.7
    assert kwargs["role"] == "model_coder"
    assert kwargs["overrides"] is None


# ---------------------------------------------------------------------------
# §17.581 — tool_call_until_args (structured-args sibling)
# ---------------------------------------------------------------------------

_TOOLS = [SimpleNamespace(name="emit_x")]


def _tc_resp(args, *, success=True):
    """Response with .tool_calls[0].arguments, matching read_tool_args."""
    tool_calls = (
        [] if args is None else [SimpleNamespace(arguments=args)]
    )
    return SimpleNamespace(success=success, tool_calls=tool_calls, error=None)


async def _tc_call(mock, *, draws=3):
    return await tool_call_until_args(
        mock, _MESSAGES, _TOOLS, {"role": "model_general", "overrides": None},
        temperature=0.3, max_tokens=8192, draws=draws, label="phase2_compile",
    )


@pytest.mark.smoke
async def test_tc_valid_first_draw_no_retry():
    mock = AsyncMock(side_effect=[_tc_resp({"compiled_prompt": "x"})])
    resp = await _tc_call(mock)
    assert resp.tool_calls[0].arguments == {"compiled_prompt": "x"}
    assert mock.call_count == 1


@pytest.mark.smoke
async def test_tc_no_args_then_valid_redraws():
    """The §17.581 failure mode: success but no tool args (reasoning-model
    prose), then a parseable draw → recovered."""
    mock = AsyncMock(side_effect=[_tc_resp(None), _tc_resp({"compiled_prompt": "x"})])
    resp = await _tc_call(mock)
    assert resp.tool_calls[0].arguments == {"compiled_prompt": "x"}
    assert mock.call_count == 2


@pytest.mark.smoke
async def test_tc_all_argless_exhausts_and_returns_last():
    """All draws argless → returns the last so the caller's read_tool_args→None
    handling (fail_job) still applies."""
    mock = AsyncMock(side_effect=[_tc_resp(None), _tc_resp(None), _tc_resp(None)])
    resp = await _tc_call(mock)
    assert resp.tool_calls == []
    assert mock.call_count == 3


@pytest.mark.smoke
async def test_tc_hard_failure_returns_immediately():
    """success=False → surface at once, don't burn re-draws."""
    mock = AsyncMock(side_effect=[_tc_resp(None, success=False)])
    resp = await _tc_call(mock)
    assert resp.success is False
    assert mock.call_count == 1


@pytest.mark.smoke
async def test_tc_forwards_messages_tools_and_budget():
    mock = AsyncMock(side_effect=[_tc_resp({"compiled_prompt": "x"})])
    await _tc_call(mock)
    _, kwargs = mock.call_args
    assert kwargs["messages"] is _MESSAGES
    assert kwargs["tools"] is _TOOLS
    assert kwargs["max_tokens"] == 8192
    assert kwargs["temperature"] == 0.3
    assert kwargs["role"] == "model_general"
