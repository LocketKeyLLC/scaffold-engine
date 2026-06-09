"""§17.465 — node generation redraws on a thinking-model empty draw and uses
the generous node_generation_max_tokens budget.

Root cause (job 4e3b8f01 nodes T3/T5): the node-exec chat call used
model_router.chat()'s default max_tokens=4096. For a thinking model
(qwen3.5:397b-cloud) num_predict is a SHARED reasoning+content budget, so a long
chain of thought returned empty/truncated content. The verifier rightly rejected
it, and the W.1 retry loop re-ran at the same 4096 cap — never recovering, so the
job blocked. The fix routes generation through chat_until_nonempty with an 8192
budget and a generation-layer redraw before the verifier/retry ever runs.

These tests patch the LLM seam (execution_agent.model_router.chat), capture the
budget, and short-circuit at a defined seam, asserting the wiring contract — not
the full execute lifecycle.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
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
    """Raised at a defined post-generation seam to bail without driving the
    full execute lifecycle."""


async def _drive(job_id, chat_mock, *, verify_mock=None):
    """Run execute_next_node with the LLM seam replaced; the patch stack
    matches the other test_execution_agent_* wiring tests. ``verify_mock``
    (the generic verifier) is the short-circuit seam reached only once
    generation produced a non-empty output."""
    if verify_mock is None:
        verify_mock = AsyncMock(side_effect=_Sentinel())
    db = AsyncMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    with patch.object(
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
        execution_agent.model_router, "chat", chat_mock,
    ):
        # skip_optimize keeps optimize_prompt out of the picture. The verifier
        # seam raises _Sentinel once generation produced a non-empty output;
        # it is reached only AFTER the (post-generation) verification section,
        # which sits outside execute_next_node's generation try/except, so it
        # propagates here. By that point the chat-call contract we assert on is
        # fully recorded — swallow it.
        try:
            await execute_next_node(job_id, skip_optimize=True)
        except _Sentinel:
            pass


@pytest.mark.asyncio
@pytest.mark.smoke
async def test_node_generation_redraws_on_empty_and_uses_generous_budget():
    """Draw 1 empty (thinking overran) → draw 2 lands; the budget forwarded is
    node_generation_max_tokens (8192), NOT the bare chat() default 4096."""
    job_id = "4e3b8f01-145c-4c54-a0f6-5639101ee1ca"
    captured = {"max_tokens": [], "calls": 0}

    async def _chat(messages, model=None, **kw):
        captured["calls"] += 1
        captured["max_tokens"].append(kw.get("max_tokens"))
        if captured["calls"] == 1:
            return _ok("")                       # thinking-model empty draw
        return _ok("real SAS pool runbook")      # redraw lands

    await _drive(job_id, _chat)  # _verify_output sentinel bails after generation

    assert captured["calls"] == 2, (
        "expected a generation-layer redraw after the empty draw; "
        f"chat was called {captured['calls']}x"
    )
    assert captured["max_tokens"] == [
        settings.node_generation_max_tokens,
        settings.node_generation_max_tokens,
    ], "node generation must use node_generation_max_tokens, not the 4096 default"
    assert settings.node_generation_max_tokens >= 8192


@pytest.mark.asyncio
@pytest.mark.smoke
async def test_node_generation_no_redraw_when_first_draw_nonempty():
    """A healthy first draw must NOT trigger a wasted second call — chat is hit
    exactly once before the verifier seam."""
    job_id = "4e3b8f01-145c-4c54-a0f6-5639101ee1ca"
    calls = {"n": 0}

    async def _chat(messages, model=None, **kw):
        calls["n"] += 1
        return _ok("real runbook content")

    await _drive(job_id, _chat)  # sentinel verifier bails right after generation

    assert calls["n"] == 1, f"healthy first draw should not redraw; got {calls['n']} calls"
