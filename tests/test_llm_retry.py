"""§17.464 — tests for the shared retry-on-empty LLM guard."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.utils.llm_retry import chat_until_nonempty, generate_until_nonempty

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
# §17.876 — think-off rescue draw (opt-in last resort after all draws empty)
# ---------------------------------------------------------------------------


@pytest.mark.smoke
async def test_chat_think_off_rescue_fires_after_all_empty():
    """Live incident: a big fix prompt drove near-deterministic chain-of-thought
    past the budget on ALL draws (temp 0.3) → "(no fix returned)". The rescue
    draw re-issues the call with think=False and its answer is returned."""
    mock = AsyncMock(side_effect=[_resp(""), _resp(""), _resp(""), _resp("rescued")])
    resp = await chat_until_nonempty(
        mock, _MESSAGES, {"role": "model_general"},
        temperature=0.3, max_tokens=8192, label="assist_fix",
        think_off_rescue=True,
    )
    assert resp.text == "rescued"
    assert mock.call_count == 4
    _, kwargs = mock.call_args
    assert kwargs["think"] is False


@pytest.mark.smoke
async def test_chat_think_off_rescue_not_fired_when_a_draw_lands():
    """A usable normal draw wins — no rescue call, no think kwarg."""
    mock = AsyncMock(side_effect=[_resp(""), _resp("real")])
    resp = await chat_until_nonempty(
        mock, _MESSAGES, {"role": "model_general"},
        temperature=0.3, max_tokens=8192, label="assist_fix",
        think_off_rescue=True,
    )
    assert resp.text == "real"
    assert mock.call_count == 2
    _, kwargs = mock.call_args
    assert "think" not in kwargs


@pytest.mark.smoke
async def test_chat_rescue_empty_returns_original_last_draw():
    """An unusable rescue must not change caller error semantics — the last
    normal (empty) draw is returned, exactly as without the rescue."""
    mock = AsyncMock(side_effect=[_resp(""), _resp(""), _resp(""), _resp("")])
    resp = await chat_until_nonempty(
        mock, _MESSAGES, {"role": "model_general"},
        temperature=0.3, max_tokens=8192, label="assist_fix",
        think_off_rescue=True,
    )
    assert (resp.text or "").strip() == ""
    assert mock.call_count == 4


@pytest.mark.smoke
async def test_chat_default_has_no_rescue():
    """Opt-in only: without think_off_rescue the behavior is byte-identical to
    the pre-§17.876 guard (3 draws, no fourth call)."""
    mock = AsyncMock(side_effect=[_resp(""), _resp(""), _resp("")])
    resp = await _chat_call(mock)
    assert (resp.text or "").strip() == ""
    assert mock.call_count == 3


@pytest.mark.smoke
async def test_generate_think_off_rescue_fires_after_all_empty():
    """generate_until_nonempty sibling gets the same opt-in rescue."""
    mock = AsyncMock(side_effect=[_resp(""), _resp(""), _resp(""), _resp("rescued")])
    resp = await generate_until_nonempty(
        mock, "p", {"role": "model_general"},
        system="s", temperature=0.3, max_tokens=8192, label="t",
        think_off_rescue=True,
    )
    assert resp.text == "rescued"
    assert mock.call_count == 4
    _, kwargs = mock.call_args
    assert kwargs["think"] is False
