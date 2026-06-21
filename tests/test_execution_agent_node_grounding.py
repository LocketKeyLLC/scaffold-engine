"""§17.570 — per-node grounding loop (_maybe_node_grounding).

Opt-in (default OFF), detect+correct in place, fail-soft. Patches the lazily
imported score_faithfulness + cove_revise at their source modules.
"""
from unittest.mock import AsyncMock

import pytest

import app.modules.cove as cov
import app.modules.faithfulness as fa
from app.config import settings
from app.modules import execution_agent as ea


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.setattr(settings, "grounding_min_score", 0.7)


@pytest.mark.asyncio
async def test_disabled_is_noop(monkeypatch):
    monkeypatch.setattr(settings, "node_grounding_enabled", False)
    sf = AsyncMock()
    monkeypatch.setattr(fa, "score_faithfulness", sf)
    out = await ea._maybe_node_grounding("j", "n", "output", "evidence", tool="LLM")
    assert out == "output"
    sf.assert_not_awaited()


@pytest.mark.asyncio
async def test_low_corrects_in_place(monkeypatch):
    monkeypatch.setattr(settings, "node_grounding_enabled", True)
    monkeypatch.setattr(fa, "score_faithfulness", AsyncMock(return_value={
        "score": 0.4, "supported": 2, "total": 5, "unsupported_claims": ["x"]}))
    cove = AsyncMock(return_value={"revised": "grounded", "changed": True})
    monkeypatch.setattr(cov, "cove_revise", cove)
    out = await ea._maybe_node_grounding("j", "n", "drifty", "evidence", tool="LLM")
    assert out == "grounded"
    cove.assert_awaited_once()


@pytest.mark.asyncio
async def test_high_unchanged_no_cove(monkeypatch):
    monkeypatch.setattr(settings, "node_grounding_enabled", True)
    monkeypatch.setattr(fa, "score_faithfulness", AsyncMock(return_value={
        "score": 0.95, "supported": 19, "total": 20, "unsupported_claims": []}))
    cove = AsyncMock()
    monkeypatch.setattr(cov, "cove_revise", cove)
    out = await ea._maybe_node_grounding("j", "n", "good", "evidence", tool="LLM")
    assert out == "good"
    cove.assert_not_awaited()


@pytest.mark.asyncio
async def test_none_score_is_noop(monkeypatch):
    monkeypatch.setattr(settings, "node_grounding_enabled", True)
    monkeypatch.setattr(fa, "score_faithfulness", AsyncMock(return_value=None))
    out = await ea._maybe_node_grounding("j", "n", "output", "evidence", tool="LLM")
    assert out == "output"


@pytest.mark.asyncio
async def test_low_but_cove_no_change_keeps_original(monkeypatch):
    monkeypatch.setattr(settings, "node_grounding_enabled", True)
    monkeypatch.setattr(fa, "score_faithfulness", AsyncMock(return_value={
        "score": 0.3, "supported": 1, "total": 4, "unsupported_claims": ["x"]}))
    monkeypatch.setattr(cov, "cove_revise", AsyncMock(
        return_value={"revised": "x", "changed": False}))
    out = await ea._maybe_node_grounding("j", "n", "orig", "evidence", tool="LLM")
    assert out == "orig"   # changed=False → keep the original
