"""Unit tests for app/modules/assist_session_map.py.

Mocks `redis.asyncio.from_url` so the suite stays Redis-free; integration
tests cover the actual round-trip.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import assist_session_map


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Each test gets a fresh module-level _redis client so mock state
    doesn't leak between tests."""
    assist_session_map._redis = None
    yield
    assist_session_map._redis = None


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_remember_then_recall_round_trips():
    fake = MagicMock()
    fake.get = AsyncMock(return_value=None)
    fake.set = AsyncMock()
    with patch.object(assist_session_map, "_client", AsyncMock(return_value=fake)):
        await assist_session_map.remember(
            "chat-1", session_id="sid-1", last_node_key="T1",
        )

    args, kwargs = fake.set.call_args
    key, payload = args
    assert key == "assist:chatmap:v1:chat-1"
    assert json.loads(payload) == {"session_id": "sid-1", "last_node_key": "T1"}
    assert kwargs["ex"] > 0  # TTL set


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_remember_preserves_last_node_when_omitted():
    """Calling remember(session_id=...) without last_node_key must keep
    the existing one — typical flow: /assist start sets sid, then
    /assist next refreshes node, then /assist next on a no-claim
    response should not wipe last_node_key."""
    existing = json.dumps({"session_id": "sid-1", "last_node_key": "T1"})
    fake = MagicMock()
    fake.get = AsyncMock(return_value=existing)
    fake.set = AsyncMock()
    with patch.object(assist_session_map, "_client", AsyncMock(return_value=fake)):
        await assist_session_map.remember("chat-1", session_id="sid-1")

    payload = json.loads(fake.set.call_args.args[1])
    assert payload == {"session_id": "sid-1", "last_node_key": "T1"}


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_recall_missing_returns_none():
    fake = MagicMock()
    fake.get = AsyncMock(return_value=None)
    with patch.object(assist_session_map, "_client", AsyncMock(return_value=fake)):
        out = await assist_session_map.recall("chat-x")
    assert out is None


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_recall_returns_dict_when_present():
    fake = MagicMock()
    fake.get = AsyncMock(return_value=json.dumps(
        {"session_id": "sid-1", "last_node_key": "T2"}
    ))
    with patch.object(assist_session_map, "_client", AsyncMock(return_value=fake)):
        out = await assist_session_map.recall("chat-1")
    assert out == {"session_id": "sid-1", "last_node_key": "T2"}


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_redis_failure_swallowed_in_remember():
    """The pipeline must keep working when Redis is down — explicit-arg
    flow remains the fallback. remember() should log+swallow, not raise."""
    fake = MagicMock()
    fake.get = AsyncMock(side_effect=ConnectionError("redis down"))
    with patch.object(assist_session_map, "_client", AsyncMock(return_value=fake)):
        # No exception — recall() returning None is the contract callers rely on.
        await assist_session_map.remember("chat-1", session_id="sid-1")


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_redis_failure_returns_none_in_recall():
    fake = MagicMock()
    fake.get = AsyncMock(side_effect=ConnectionError("redis down"))
    with patch.object(assist_session_map, "_client", AsyncMock(return_value=fake)):
        assert await assist_session_map.recall("chat-1") is None


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_forget_deletes_key():
    fake = MagicMock()
    fake.delete = AsyncMock()
    with patch.object(assist_session_map, "_client", AsyncMock(return_value=fake)):
        await assist_session_map.forget("chat-1")
    fake.delete.assert_awaited_once_with("assist:chatmap:v1:chat-1")
