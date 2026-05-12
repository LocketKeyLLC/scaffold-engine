"""Tests for the verifier-cache wiring inside _verify_output.

The cache itself is exercised in test_llm_response_cache.py; this module
verifies that _verify_output (a) skips the LLM on a cache hit, (b) puts
on pass, (c) does not put on fail, and (d) bypasses the cache entirely
when the gate is off.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.modules import execution_verify
from app.providers.base import ModelResponse, ToolCall


def _ok_with_args(args: dict):
    return ModelResponse(
        text="", model="fake", success=True,
        tool_calls=[ToolCall(id="t0", name="record_verification", arguments=args)],
    )


@pytest.fixture
def gate_on(monkeypatch):
    monkeypatch.setattr(
        "app.utils.llm_response_cache.settings.cache_llm_responses", True,
    )


@pytest.fixture
def fresh_cache_singleton(monkeypatch):
    """Reset the cache singleton so tests don't bleed state."""
    monkeypatch.setattr("app.utils.llm_response_cache._cache", None)


@pytest.fixture
def fake_redis(monkeypatch, fresh_cache_singleton):
    """In-memory fake Redis bound to the verifier-cache singleton."""
    store: dict[bytes, bytes] = {}
    mock_redis = AsyncMock()

    async def _set(key, value, ex=None):
        k = key.encode() if isinstance(key, str) else key
        store[k] = value
    async def _get(key):
        k = key.encode() if isinstance(key, str) else key
        return store.get(k)
    async def _delete(key):
        k = key.encode() if isinstance(key, str) else key
        store.pop(k, None)
    mock_redis.set = AsyncMock(side_effect=_set)
    mock_redis.get = AsyncMock(side_effect=_get)
    mock_redis.delete = AsyncMock(side_effect=_delete)

    from app.utils.llm_response_cache import get_verifier_cache
    cache = get_verifier_cache()
    cache._redis = mock_redis
    # Reset counters so we can assert hits/puts cleanly.
    cache._hits = 0
    cache._misses = 0
    cache._puts = 0
    cache._skipped = 0
    return cache


@pytest.mark.asyncio
async def test_second_identical_call_short_circuits_llm(gate_on, fake_redis):
    """First call hits the LLM and writes; second call returns the cached tuple."""
    call_count = 0

    async def _fake_tool_call(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return _ok_with_args({"pass": True, "reason": "ok", "confidence": 0.9})

    with patch.object(execution_verify.model_router, "tool_call", side_effect=_fake_tool_call):
        first = await execution_verify._verify_output("task", "output")
        second = await execution_verify._verify_output("task", "output")

    assert first == ("pass", "ok", 0.9)
    assert second == ("pass", "ok", 0.9)
    assert call_count == 1, "second call should not have hit the LLM"
    assert fake_redis.stats()["hits"] == 1
    assert fake_redis.stats()["puts"] == 1


@pytest.mark.asyncio
async def test_fail_verdict_is_not_cached(gate_on, fake_redis):
    """A fail verdict must re-evaluate on the next call (W.1 retry has fresh context)."""
    call_count = 0

    async def _fake_tool_call(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return _ok_with_args({"pass": False, "reason": "missing", "confidence": 0.4})

    with patch.object(execution_verify.model_router, "tool_call", side_effect=_fake_tool_call):
        await execution_verify._verify_output("task", "output")
        await execution_verify._verify_output("task", "output")

    assert call_count == 2
    assert fake_redis.stats()["puts"] == 0


@pytest.mark.asyncio
async def test_different_output_misses(gate_on, fake_redis):
    """Different output text → different cache key → LLM call."""
    call_count = 0

    async def _fake_tool_call(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return _ok_with_args({"pass": True, "reason": "ok", "confidence": 0.9})

    with patch.object(execution_verify.model_router, "tool_call", side_effect=_fake_tool_call):
        await execution_verify._verify_output("task", "output A")
        await execution_verify._verify_output("task", "output B")

    assert call_count == 2
    assert fake_redis.stats()["puts"] == 2


@pytest.mark.asyncio
async def test_gate_off_skips_cache_entirely(monkeypatch, fresh_cache_singleton):
    """With cache_llm_responses=False, every call hits the LLM and Redis is untouched."""
    monkeypatch.setattr(
        "app.utils.llm_response_cache.settings.cache_llm_responses", False,
    )
    mock_redis = AsyncMock()
    from app.utils.llm_response_cache import get_verifier_cache
    cache = get_verifier_cache()
    cache._redis = mock_redis

    call_count = 0

    async def _fake_tool_call(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return _ok_with_args({"pass": True, "reason": "ok", "confidence": 0.9})

    with patch.object(execution_verify.model_router, "tool_call", side_effect=_fake_tool_call):
        await execution_verify._verify_output("task", "output")
        await execution_verify._verify_output("task", "output")

    assert call_count == 2
    mock_redis.get.assert_not_awaited()
    mock_redis.set.assert_not_awaited()


@pytest.mark.asyncio
async def test_cache_failure_falls_through_to_llm(monkeypatch, gate_on, fresh_cache_singleton):
    """If Redis errors on get, _verify_output must still produce a verdict."""
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(side_effect=ConnectionError("redis down"))
    mock_redis.set = AsyncMock(side_effect=ConnectionError("redis down"))

    from app.utils.llm_response_cache import get_verifier_cache
    cache = get_verifier_cache()
    cache._redis = mock_redis

    async def _fake_tool_call(*args, **kwargs):
        return _ok_with_args({"pass": True, "reason": "ok", "confidence": 0.9})

    with patch.object(execution_verify.model_router, "tool_call", side_effect=_fake_tool_call):
        out = await execution_verify._verify_output("task", "output")

    assert out == ("pass", "ok", 0.9)
