"""§17.464 — tests for the shared retry-on-empty LLM guard."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.utils.llm_retry import generate_until_nonempty

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
