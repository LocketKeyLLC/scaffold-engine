"""§17.776 — per-node token streaming.

When ``settings.node_token_streaming_enabled`` is on AND the caller passes a
``token_q``, the node-generation phase streams content deltas via
``model_router.stream_chat`` and pushes one pre-formatted ``node_token`` SSE
frame per chunk onto the queue. The final ``output`` still equals the
concatenation of the streamed deltas, so downstream verify/persist is unchanged.
When the stream yields nothing, it falls back to the non-stream
``chat_until_nonempty`` path (preserving the §17.465 empty-guard AND cost
tracking). With the valve off (or no queue), generation takes the byte-identical
non-stream path and ``stream_chat`` is never touched.

These patch the LLM seams (execution_agent.model_router.stream_chat / .chat) and
short-circuit at the verifier seam, asserting the wiring contract — not the full
execute lifecycle. Harness mirrors test_execution_agent_empty_redraw.py.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.modules import execution_agent
from app.modules.execution_agent import execute_next_node


@asynccontextmanager
async def _fake_session(db):
    yield db


def _fake_session_factory(db):
    return lambda: _fake_session(db)


def _ok(text, *, success=True):
    return SimpleNamespace(text=text, success=success, error=None, model="m")


def _node_row():
    return {
        "id": "node-uuid",
        "node_key": "T3",
        "title": "Configure SAS storage pools",
        "tool": "LLM",
        "prompt_template": "Configure the SAS storage pools.",
        "domain": None,
        "depends_on": [],
        "assigned_model": None,
        "retry_count": 0,
        "last_verification_reason": None,
    }


def _job_row(job_id):
    return {
        "id": job_id, "status": "running",
        "refined_brief": {"description": "Secure Proxmox HomeLab"},
    }


class _Sentinel(Exception):
    """Raised at the post-generation verifier seam to bail without driving the
    full execute lifecycle. The token-stream contract is fully recorded by
    then."""


async def _drive(job_id, *, stream_mock=None, chat_mock=None, token_q=None,
                 streaming_enabled=True):
    verify_mock = AsyncMock(side_effect=_Sentinel())
    db = AsyncMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    if stream_mock is None:
        async def stream_mock(messages, **kw):  # noqa: ARG001
            for _ in ():
                yield ""
    if chat_mock is None:
        chat_mock = AsyncMock(return_value=_ok("fallback content"))
    with patch.object(
        execution_agent.settings, "node_token_streaming_enabled", streaming_enabled,
    ), patch.object(
        execution_agent, "async_session", _fake_session_factory(db),
    ), patch.object(
        execution_agent, "_get_job", AsyncMock(return_value=_job_row(job_id)),
    ), patch.object(
        execution_agent, "_get_next_node", AsyncMock(return_value=_node_row()),
    ), patch.object(
        execution_agent, "_fetch_upstream_outputs", AsyncMock(return_value={}),
    ), patch.object(
        execution_agent, "_fetch_rag_context", AsyncMock(return_value=None),
    ), patch.object(
        execution_agent, "_log_execution", AsyncMock(),
    ), patch.object(
        execution_agent, "_set_node_status", AsyncMock(),
    ), patch.object(
        execution_agent, "_verify_output", verify_mock,
    ), patch.object(
        execution_agent.model_router, "stream_chat", stream_mock,
    ), patch.object(
        execution_agent.model_router, "chat", chat_mock,
    ):
        try:
            await execute_next_node(job_id, skip_optimize=True, token_q=token_q)
        except _Sentinel:
            pass
    return chat_mock


def _drain(q: asyncio.Queue) -> list[dict]:
    """Pull all frames off the queue and decode the node_token payloads."""
    frames = []
    while not q.empty():
        raw = q.get_nowait()
        # SSE frame: "event: node_token\ndata: {...}\n\n"
        assert raw.startswith("event: node_token\n"), raw
        data_line = raw.split("data: ", 1)[1].strip()
        frames.append(json.loads(data_line))
    return frames


@pytest.mark.asyncio
@pytest.mark.smoke
async def test_node_streams_tokens_to_queue():
    """Each stream_chat delta becomes one node_token frame on token_q, and the
    concatenated deltas are what generation returns downstream."""
    job_id = "4e3b8f01-145c-4c54-a0f6-5639101ee1ca"
    deltas = ["Step 1: ", "create the pool", " with zpool."]

    async def _stream(messages, **kw):  # noqa: ARG001
        for d in deltas:
            yield d

    q: asyncio.Queue = asyncio.Queue()
    await _drive(job_id, stream_mock=_stream, token_q=q)

    frames = _drain(q)
    assert [f["delta"] for f in frames] == deltas
    assert all(f["node_key"] == "T3" and f["job_id"] == job_id for f in frames)


@pytest.mark.asyncio
@pytest.mark.smoke
async def test_empty_stream_falls_back_to_recorded_chat():
    """A stream that yields nothing (thinking-model emptiness) must fall back to
    the non-stream chat_until_nonempty path so the draw is empty-guarded AND
    cost-tracked. No node_token frames are emitted."""
    job_id = "4e3b8f01-145c-4c54-a0f6-5639101ee1ca"

    async def _empty_stream(messages, **kw):  # noqa: ARG001
        return
        yield  # pragma: no cover — makes this an async generator

    chat_mock = AsyncMock(return_value=_ok("recovered runbook"))
    q: asyncio.Queue = asyncio.Queue()
    await _drive(job_id, stream_mock=_empty_stream, chat_mock=chat_mock, token_q=q)

    assert chat_mock.await_count >= 1, "empty stream must fall back to chat()"
    assert q.empty(), "no node_token frames should be emitted for an empty stream"


@pytest.mark.asyncio
@pytest.mark.smoke
async def test_valve_off_never_streams():
    """With the valve off, generation takes the non-stream path: stream_chat is
    never called and chat() carries the draw — byte-identical to pre-§17.776."""
    job_id = "4e3b8f01-145c-4c54-a0f6-5639101ee1ca"
    stream_called = {"n": 0}

    async def _stream(messages, **kw):  # noqa: ARG001
        stream_called["n"] += 1
        yield "should not happen"

    chat_mock = AsyncMock(return_value=_ok("normal content"))
    q: asyncio.Queue = asyncio.Queue()
    await _drive(
        job_id, stream_mock=_stream, chat_mock=chat_mock, token_q=q,
        streaming_enabled=False,
    )

    assert stream_called["n"] == 0, "valve off must not invoke stream_chat"
    assert chat_mock.await_count >= 1, "valve off must use the non-stream chat path"
    assert q.empty()


@pytest.mark.asyncio
@pytest.mark.smoke
async def test_no_queue_never_streams_even_when_valve_on():
    """Valve on but the caller passes token_q=None (e.g. the parallel-frontier
    path today) → non-stream path, stream_chat untouched."""
    job_id = "4e3b8f01-145c-4c54-a0f6-5639101ee1ca"
    stream_called = {"n": 0}

    async def _stream(messages, **kw):  # noqa: ARG001
        stream_called["n"] += 1
        yield "nope"

    chat_mock = AsyncMock(return_value=_ok("normal content"))
    await _drive(
        job_id, stream_mock=_stream, chat_mock=chat_mock, token_q=None,
        streaming_enabled=True,
    )

    assert stream_called["n"] == 0, "no queue → must not stream"
    assert chat_mock.await_count >= 1
