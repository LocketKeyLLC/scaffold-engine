"""§17.759 — durable-only cross-component sharing: a sibling contributes only its
DURABLE infrastructure facts (cached), not transient states or component-specific
detail. Fail-soft to sharing all facts (the §17.757 behavior).
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.modules import assist_agent, assist_guide, assist_memory
from app.providers.base import ToolCall


def _resp(args):
    return types.SimpleNamespace(
        text="", success=True, error=None,
        tool_calls=[ToolCall(id="t", name="report_durable_facts", arguments=args)])


# ── classify_durable_facts ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_classify_returns_indices():
    with patch("app.modules.assist_guide.model_router.tool_call",
               new=AsyncMock(return_value=_resp({"durable_indices": [0, 2]}))):
        assert await assist_guide.classify_durable_facts(facts=["hw", "transient", "net"]) == [0, 2]


@pytest.mark.asyncio
async def test_classify_none_on_failure():
    with patch("app.modules.assist_guide.model_router.tool_call",
               new=AsyncMock(side_effect=RuntimeError("down"))):
        assert await assist_guide.classify_durable_facts(facts=["a"]) is None


@pytest.mark.asyncio
async def test_classify_none_when_unparsed():
    bad = types.SimpleNamespace(text="", success=True, error=None, tool_calls=[])
    with patch("app.modules.assist_guide.model_router.tool_call",
               new=AsyncMock(return_value=bad)):
        assert await assist_guide.classify_durable_facts(facts=["a"]) is None


# ── _durable_facts_for_session (cache) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_cache_hit_skips_classify():
    meta = {"environment": {"facts": ["a", "b"], "durable_facts": ["a"], "durable_facts_n": 2}}
    with patch.object(assist_guide, "classify_durable_facts", new=AsyncMock()) as cl:
        out = await assist_agent._durable_facts_for_session(
            session_id="s", metadata=meta, db=AsyncMock())
    assert out == ["a"]
    cl.assert_not_awaited()   # served from cache, no LLM at read time


@pytest.mark.asyncio
async def test_cache_miss_classifies_and_writes():
    meta = {"environment": {"facts": ["host has 2 NICs", "link is DOWN right now"]}}
    db = AsyncMock()
    with patch.object(assist_guide, "classify_durable_facts",
                      new=AsyncMock(return_value=[0])) as cl:
        out = await assist_agent._durable_facts_for_session(
            session_id="s", metadata=meta, db=db)
    assert out == ["host has 2 NICs"]          # transient dropped
    cl.assert_awaited_once()
    db.execute.assert_awaited()                # cached back to the session
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_classifier_failure_falls_back_to_all_facts():
    meta = {"environment": {"facts": ["a", "b", "c"]}}
    db = AsyncMock()
    with patch.object(assist_guide, "classify_durable_facts",
                      new=AsyncMock(return_value=None)):
        out = await assist_agent._durable_facts_for_session(
            session_id="s", metadata=meta, db=db)
    assert out == ["a", "b", "c"]              # fail-soft: share all
    db.execute.assert_not_awaited()            # no cache write on fallback


# ── _sibling_facts routes through durable-only ──────────────────────────────


class _Rows:
    def __init__(self, rows): self._rows = rows
    def mappings(self): return self
    def all(self): return self._rows
    def scalar(self): return "umbrella-1"


@pytest.mark.asyncio
async def test_sibling_facts_uses_durable_subset(monkeypatch):
    monkeypatch.setattr(settings, "assist_cross_component_facts_enabled", True, raising=False)
    monkeypatch.setattr(settings, "assist_cross_component_durable_only", True, raising=False)
    monkeypatch.setattr(settings, "assist_cross_component_facts_cap", 40, raising=False)
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _Rows([]),                                        # parent lookup (.scalar())
        _Rows([{"id": "sib1", "metadata": {"environment": {"facts": ["x"]}}}]),  # siblings
    ])
    with patch.object(assist_memory, "_durable_facts_for_session",
                      new=AsyncMock(return_value=["host CPU is Xeon"])) as durable:
        out = await assist_agent._sibling_facts(job_id="me", db=db)
    assert out == ["host CPU is Xeon"]
    durable.assert_awaited_once()
