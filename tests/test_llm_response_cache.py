"""Tests for app.utils.llm_response_cache — verifier-verdict cache."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from app.utils.llm_response_cache import (
    VerifierCache,
    _KEY_PREFIX,
    make_key,
)


_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "pass": {"type": "boolean"},
        "reason": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["pass", "reason", "confidence"],
}
_MESSAGES = [
    {"role": "system", "content": "you are a verifier"},
    {"role": "user", "content": "TASK: foo\n\nOUTPUT:\nbar"},
]


# ---------------------------------------------------------------------------
# make_key
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestMakeKey:
    def test_basic_shape(self):
        k = make_key(_MESSAGES, _TOOL_SCHEMA, "qwen2.5:7b", 0.0)
        assert k.startswith(f"{_KEY_PREFIX}:qwen2.5:7b:")
        # SHA256 hex = 64 chars
        assert len(k.rsplit(":", 1)[1]) == 64

    def test_deterministic(self):
        a = make_key(_MESSAGES, _TOOL_SCHEMA, "qwen2.5:7b", 0.0)
        b = make_key(_MESSAGES, _TOOL_SCHEMA, "qwen2.5:7b", 0.0)
        assert a == b

    def test_message_change_changes_key(self):
        other = [_MESSAGES[0], {"role": "user", "content": "different"}]
        assert make_key(_MESSAGES, _TOOL_SCHEMA, "m", 0.0) != make_key(other, _TOOL_SCHEMA, "m", 0.0)

    def test_model_change_changes_key(self):
        a = make_key(_MESSAGES, _TOOL_SCHEMA, "m1", 0.0)
        b = make_key(_MESSAGES, _TOOL_SCHEMA, "m2", 0.0)
        assert a != b

    def test_temperature_change_changes_key(self):
        a = make_key(_MESSAGES, _TOOL_SCHEMA, "m", 0.0)
        b = make_key(_MESSAGES, _TOOL_SCHEMA, "m", 0.1)
        assert a != b

    def test_tool_schema_change_changes_key(self):
        other_schema = {**_TOOL_SCHEMA, "title": "different"}
        a = make_key(_MESSAGES, _TOOL_SCHEMA, "m", 0.0)
        b = make_key(_MESSAGES, other_schema, "m", 0.0)
        assert a != b

    def test_dict_ordering_does_not_affect_key(self):
        # canonical encoding is sort_keys=True
        msg_a = [{"role": "user", "content": "x"}]
        msg_b = [{"content": "x", "role": "user"}]
        assert make_key(msg_a, _TOOL_SCHEMA, "m", 0.0) == make_key(msg_b, _TOOL_SCHEMA, "m", 0.0)


# ---------------------------------------------------------------------------
# VerifierCache — get / put round-trip with mocked Redis
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_miss_returns_none(monkeypatch):
    monkeypatch.setattr("app.utils.llm_response_cache.settings.cache_llm_responses", True)
    cache = VerifierCache()
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)
    cache._redis = mock_redis

    out = await cache.get(_MESSAGES, _TOOL_SCHEMA, "m", 0.0)
    assert out is None
    assert cache._misses == 1
    assert cache._hits == 0


@pytest.mark.asyncio
async def test_get_hit_returns_tuple(monkeypatch):
    monkeypatch.setattr("app.utils.llm_response_cache.settings.cache_llm_responses", True)
    cache = VerifierCache()
    payload = json.dumps({"status": "pass", "reason": "ok", "confidence": 0.91})
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=payload.encode("utf-8"))
    cache._redis = mock_redis

    out = await cache.get(_MESSAGES, _TOOL_SCHEMA, "m", 0.0)
    assert out == ("pass", "ok", 0.91)
    assert cache._hits == 1


@pytest.mark.asyncio
async def test_get_corrupt_payload_drops_key(monkeypatch):
    monkeypatch.setattr("app.utils.llm_response_cache.settings.cache_llm_responses", True)
    cache = VerifierCache()
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=b"not json")
    mock_redis.delete = AsyncMock()
    cache._redis = mock_redis

    out = await cache.get(_MESSAGES, _TOOL_SCHEMA, "m", 0.0)
    assert out is None
    assert cache._misses == 1
    mock_redis.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_bad_status_field_drops_key(monkeypatch):
    monkeypatch.setattr("app.utils.llm_response_cache.settings.cache_llm_responses", True)
    cache = VerifierCache()
    payload = json.dumps({"status": "weird", "reason": "x", "confidence": 0.5})
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=payload.encode("utf-8"))
    mock_redis.delete = AsyncMock()
    cache._redis = mock_redis

    out = await cache.get(_MESSAGES, _TOOL_SCHEMA, "m", 0.0)
    assert out is None
    mock_redis.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_redis_error_fails_open(monkeypatch, caplog):
    monkeypatch.setattr("app.utils.llm_response_cache.settings.cache_llm_responses", True)
    cache = VerifierCache()
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(side_effect=ConnectionError("boom"))
    cache._redis = mock_redis

    with caplog.at_level("WARNING", logger="scaffold.llm_response_cache"):
        out = await cache.get(_MESSAGES, _TOOL_SCHEMA, "m", 0.0)
    assert out is None
    assert any("llm_response_cache_get_failed" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_put_pass_writes(monkeypatch):
    monkeypatch.setattr("app.utils.llm_response_cache.settings.cache_llm_responses", True)
    monkeypatch.setattr("app.utils.llm_response_cache.settings.llm_response_cache_ttl_s", 60)
    cache = VerifierCache()
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock()
    cache._redis = mock_redis

    written = await cache.put(_MESSAGES, _TOOL_SCHEMA, "m", 0.0, "pass", "ok", 0.9)
    assert written is True
    assert cache._puts == 1
    mock_redis.set.assert_awaited_once()
    # ex kwarg carries TTL
    _, kwargs = mock_redis.set.call_args
    assert kwargs.get("ex") == 60


@pytest.mark.asyncio
async def test_put_fail_is_not_cached(monkeypatch):
    monkeypatch.setattr("app.utils.llm_response_cache.settings.cache_llm_responses", True)
    cache = VerifierCache()
    mock_redis = AsyncMock()
    cache._redis = mock_redis

    written = await cache.put(_MESSAGES, _TOOL_SCHEMA, "m", 0.0, "fail", "missing", 0.7)
    assert written is False
    assert cache._puts == 0
    mock_redis.set.assert_not_awaited()


@pytest.mark.asyncio
async def test_put_redis_error_fails_open(monkeypatch, caplog):
    monkeypatch.setattr("app.utils.llm_response_cache.settings.cache_llm_responses", True)
    cache = VerifierCache()
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(side_effect=ConnectionError("boom"))
    cache._redis = mock_redis

    with caplog.at_level("WARNING", logger="scaffold.llm_response_cache"):
        written = await cache.put(_MESSAGES, _TOOL_SCHEMA, "m", 0.0, "pass", "ok", 0.9)
    assert written is False
    assert any("llm_response_cache_put_failed" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Gating — default off
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_gate_off_returns_none_without_redis_traffic(monkeypatch):
    monkeypatch.setattr("app.utils.llm_response_cache.settings.cache_llm_responses", False)
    cache = VerifierCache()
    mock_redis = AsyncMock()
    cache._redis = mock_redis

    out = await cache.get(_MESSAGES, _TOOL_SCHEMA, "m", 0.0)
    assert out is None
    assert cache._skipped == 1
    mock_redis.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_put_gate_off_is_noop(monkeypatch):
    monkeypatch.setattr("app.utils.llm_response_cache.settings.cache_llm_responses", False)
    cache = VerifierCache()
    mock_redis = AsyncMock()
    cache._redis = mock_redis

    written = await cache.put(_MESSAGES, _TOOL_SCHEMA, "m", 0.0, "pass", "ok", 0.9)
    assert written is False
    mock_redis.set.assert_not_awaited()


# ---------------------------------------------------------------------------
# Round-trip — put then get returns the same tuple
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_put_then_get_round_trip(monkeypatch):
    monkeypatch.setattr("app.utils.llm_response_cache.settings.cache_llm_responses", True)
    monkeypatch.setattr("app.utils.llm_response_cache.settings.llm_response_cache_ttl_s", 60)

    # in-memory fake Redis
    store: dict[bytes, bytes] = {}
    mock_redis = AsyncMock()

    async def _set(key, value, ex=None):
        store[key.encode() if isinstance(key, str) else key] = value
    async def _get(key):
        return store.get(key.encode() if isinstance(key, str) else key)
    mock_redis.set = AsyncMock(side_effect=_set)
    mock_redis.get = AsyncMock(side_effect=_get)

    cache = VerifierCache()
    cache._redis = mock_redis

    await cache.put(_MESSAGES, _TOOL_SCHEMA, "m", 0.0, "pass", "looks good", 0.88)
    out = await cache.get(_MESSAGES, _TOOL_SCHEMA, "m", 0.0)
    assert out == ("pass", "looks good", 0.88)
    assert cache.stats()["hits"] == 1
    assert cache.stats()["puts"] == 1
