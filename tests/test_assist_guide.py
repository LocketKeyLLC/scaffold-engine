"""§17.486 — unit tests for the Assist Mode guidance layer.

The model and the grounding helpers are always mocked: never rely on a real
LLM draw (the cloud thinking models can return success=True + empty content,
§17.465 — the empty-pitfall tests below assert chat_until_nonempty re-draws).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import assist_guide
from app.modules.prompt_assembly import (
    EXECUTION_SYSTEM_RUNBOOK,
    StepContext,
)


# ── helpers ──────────────────────────────────────────────────────────────


def _ctx(tool: str = "shell", *, upstream=None) -> StepContext:
    return StepContext(
        node_key="T3",
        title="Install the proxy",
        tool=tool,
        domain=None,
        system_prompt="sys",
        base_prompt="Install and start the reverse proxy on the host.",
        upstream_outputs=upstream or {},
        upstream_truncated_keys=[],
        grounding="",
        grounding_kind=None,
        assembled_prompt="Install and start the reverse proxy on the host.",
    )


def _resp(text: str, success: bool = True, error: str | None = None):
    r = MagicMock()
    r.success = success
    r.text = text
    r.error = error
    r.model = "fake-model"
    return r


def _tool_resp(queries, success: bool = True):
    r = MagicMock()
    r.success = success
    if success:
        call = MagicMock()
        call.arguments = {"queries": queries}
        r.tool_calls = [call]
    else:
        r.tool_calls = []
    return r


def _db():
    db = AsyncMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    return db


# ── system-prompt routing ────────────────────────────────────────────────


def test_guide_system_for_tool_shell_reuses_runbook():
    s = assist_guide.guide_system_for_tool("shell")
    assert EXECUTION_SYSTEM_RUNBOOK in s
    assert "human operator" in s.lower()


def test_guide_system_for_tool_codegen():
    assert assist_guide.guide_system_for_tool("codegen") is assist_guide.GUIDE_SYSTEM_CODEGEN


def test_guide_system_for_tool_defaults_to_noncode():
    assert assist_guide.guide_system_for_tool("LLM") is assist_guide.GUIDE_SYSTEM_NONCODE
    assert assist_guide.guide_system_for_tool(None) is assist_guide.GUIDE_SYSTEM_NONCODE


# ── generation (happy path) ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_guidance_ready_no_research():
    with patch.object(assist_guide.model_router, "chat",
                      new=AsyncMock(return_value=_resp("## Run this\n1. do it"))) as chat:
        res = await assist_guide.generate_guidance(
            ctx=_ctx("shell"), research=False, node_key="T3",
        )
    assert res["status"] == "ready"
    assert res["guidance"].startswith("## Run this")
    assert res["guidance_meta"]["research_sources"] == []
    assert res["guidance_meta"]["tool"] == "shell"
    chat.assert_awaited()  # generation happened


@pytest.mark.asyncio
async def test_generate_guidance_research_false_skips_helpers():
    with patch.object(assist_guide.model_router, "chat",
                      new=AsyncMock(return_value=_resp("walk"))), \
         patch("app.modules.execution_agent._searxng_search", new=AsyncMock()) as sx, \
         patch("app.modules.execution_agent._milvus_search", new=AsyncMock()) as mv, \
         patch.object(assist_guide.model_router, "tool_call", new=AsyncMock()) as tc:
        await assist_guide.generate_guidance(
            ctx=_ctx("codegen"), research=False, node_key="T3",
        )
    sx.assert_not_called()
    mv.assert_not_called()
    tc.assert_not_called()


# ── empty-pitfall (§17.465) ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_guidance_redraws_on_empty_then_succeeds():
    chat = AsyncMock(side_effect=[_resp(""), _resp(""), _resp("finally")])
    with patch.object(assist_guide.model_router, "chat", new=chat):
        res = await assist_guide.generate_guidance(
            ctx=_ctx("shell"), research=False, node_key="T3",
        )
    assert res["status"] == "ready"
    assert res["guidance"] == "finally"
    assert chat.await_count == 3  # re-drew past the two empties


@pytest.mark.asyncio
async def test_generate_guidance_all_empty_marks_failed():
    chat = AsyncMock(return_value=_resp(""))
    with patch.object(assist_guide.model_router, "chat", new=chat):
        res = await assist_guide.generate_guidance(
            ctx=_ctx("shell"), research=False, node_key="T3",
        )
    assert res["status"] == "failed"
    assert res["guidance"] == ""
    assert "error" in res["guidance_meta"]


@pytest.mark.asyncio
async def test_generate_guidance_hard_failure_marks_failed():
    chat = AsyncMock(return_value=_resp("", success=False, error="model down"))
    with patch.object(assist_guide.model_router, "chat", new=chat):
        res = await assist_guide.generate_guidance(
            ctx=_ctx("LLM"), research=False, node_key="T3",
        )
    assert res["status"] == "failed"
    assert res["guidance_meta"]["error"] == "model down"


# ── research pre-pass ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_research_prepass_collects_and_injects_sources():
    captured = {}

    async def _capture_chat(messages, **kw):
        captured["user"] = messages[1]["content"]
        return _resp("guided")

    with patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(return_value=_tool_resp(["nginx install ubuntu"]))), \
         patch("app.modules.execution_agent._milvus_search",
               new=AsyncMock(return_value="No knowledge base results found.")), \
         patch("app.modules.execution_agent._searxng_search",
               new=AsyncMock(return_value="[1] nginx docs\n    apt install nginx\n    https://nginx.org")), \
         patch.object(assist_guide.model_router, "chat", new=_capture_chat):
        res = await assist_guide.generate_guidance(
            ctx=_ctx("shell"), research=True, node_key="T3",
        )
    # The useful searxng result is cited; the empty milvus result is dropped.
    assert res["guidance_meta"]["research_sources"] == [
        {"query": "nginx install ubuntu", "kind": "searxng"}
    ]
    assert "## Research (confirmed" in captured["user"]
    assert "apt install nginx" in captured["user"]


@pytest.mark.asyncio
async def test_research_prepass_failsoft_on_tool_call_error():
    # tool_call fails → zero queries → guidance still generated, no sources.
    with patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(return_value=_tool_resp([], success=False))), \
         patch("app.modules.execution_agent._milvus_search", new=AsyncMock()) as mv, \
         patch("app.modules.execution_agent._searxng_search", new=AsyncMock()) as sx, \
         patch.object(assist_guide.model_router, "chat",
                      new=AsyncMock(return_value=_resp("guided anyway"))):
        res = await assist_guide.generate_guidance(
            ctx=_ctx("shell"), research=True, node_key="T3",
        )
    assert res["status"] == "ready"
    assert res["guidance_meta"]["research_sources"] == []
    mv.assert_not_called()  # no queries → no confirm calls
    sx.assert_not_called()


def test_is_useful_grounding_filters_empty_and_failures():
    assert assist_guide._is_useful_grounding("[1] real result")
    assert not assist_guide._is_useful_grounding("")
    assert not assist_guide._is_useful_grounding("No search results found.")
    assert not assist_guide._is_useful_grounding("No knowledge base results found.")
    assert not assist_guide._is_useful_grounding("SearXNG search failed: timeout")
    assert not assist_guide._is_useful_grounding("Knowledge base search failed: boom")


# ── persistence + cache ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_guidance_writes_and_commits():
    db = _db()
    await assist_guide.persist_guidance(
        session_id="s", node_key="T3", guidance="walk",
        guidance_meta={"status": "ready"}, status="ready", db=db,
    )
    db.execute.assert_awaited_once()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_read_cached_guidance_returns_none_when_not_ready():
    db = AsyncMock()
    row = {"guidance": "x", "guidance_meta": {}, "guidance_status": "failed",
           "guidance_generated_at": None}
    result = MagicMock()
    result.mappings.return_value.first.return_value = row
    db.execute = AsyncMock(return_value=result)
    assert await assist_guide.read_cached_guidance(session_id="s", node_key="T3", db=db) is None


@pytest.mark.asyncio
async def test_ensure_guidance_cache_hit_skips_llm():
    db = AsyncMock()
    with patch.object(assist_guide, "read_cached_guidance",
                      new=AsyncMock(return_value={"guidance": "cached", "status": "ready", "cached": True})), \
         patch.object(assist_guide.model_router, "chat", new=AsyncMock()) as chat, \
         patch.object(assist_guide, "persist_guidance", new=AsyncMock()) as persist:
        res = await assist_guide.ensure_guidance(
            session_id="s", node_key="T3", ctx=_ctx("shell"),
            research=False, force=False, db=db,
        )
    assert res["cached"] is True
    chat.assert_not_called()
    persist.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_guidance_force_regenerates_and_persists():
    db = AsyncMock()
    with patch.object(assist_guide, "read_cached_guidance", new=AsyncMock()) as cache, \
         patch.object(assist_guide.model_router, "chat",
                      new=AsyncMock(return_value=_resp("fresh"))), \
         patch.object(assist_guide, "persist_guidance", new=AsyncMock()) as persist:
        res = await assist_guide.ensure_guidance(
            session_id="s", node_key="T3", ctx=_ctx("shell"),
            research=False, force=True, db=db,
        )
    cache.assert_not_called()  # force bypasses the cache read
    persist.assert_awaited_once()
    assert res["status"] == "ready"
    assert res["cached"] is False


# ── explicit one-off research ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_research_one_returns_sources_and_answer():
    with patch("app.modules.execution_agent._milvus_search",
               new=AsyncMock(return_value="No knowledge base results found.")), \
         patch("app.modules.execution_agent._searxng_search",
               new=AsyncMock(return_value="[1] answer body")), \
         patch.object(assist_guide.model_router, "chat",
                      new=AsyncMock(return_value=_resp("Synthesized [1]"))):
        res = await assist_guide.research_one(question="what flag?")
    assert res["question"] == "what flag?"
    assert len(res["sources"]) == 1
    assert res["sources"][0]["kind"] == "searxng"
    assert res["answer"] == "Synthesized [1]"


@pytest.mark.asyncio
async def test_research_one_no_sources_no_synthesis():
    with patch("app.modules.execution_agent._milvus_search",
               new=AsyncMock(return_value="No knowledge base results found.")), \
         patch("app.modules.execution_agent._searxng_search",
               new=AsyncMock(return_value="No search results found.")), \
         patch.object(assist_guide.model_router, "chat", new=AsyncMock()) as chat:
        res = await assist_guide.research_one(question="obscure thing")
    assert res["sources"] == []
    assert res["answer"] is None
    chat.assert_not_called()  # nothing to synthesize from
