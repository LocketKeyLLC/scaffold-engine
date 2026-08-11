"""§17.755 — on a reset/rebuild note, auto-retract the facts describing the
abandoned system (durable host/network/storage/new-build facts kept), with a hard
cap so a mis-firing model can never wipe the ledger.
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.modules import assist_agent, assist_guide
from app.providers.base import ToolCall


def _resp(args):
    return types.SimpleNamespace(
        text="", success=True, error=None,
        tool_calls=[ToolCall(id="t", name="report_superseded_facts", arguments=args)])


def _db_with_facts(facts):
    db = AsyncMock()
    r = MagicMock()
    r.mappings.return_value.first.return_value = {"metadata": {"environment": {"facts": facts}}}
    db.execute = AsyncMock(return_value=r)
    return db


@pytest.mark.asyncio
async def test_classify_returns_indices():
    facts = ["old VM 100 boot loop", "host has 2 NICs", "new AI-VM created"]
    with patch("app.modules.assist_guide.model_router.tool_call",
               new=AsyncMock(return_value=_resp({"superseded_indices": [0]}))):
        idx = await assist_guide.classify_superseded_facts(
            note_text="reset, rebuilt the VM", facts=facts)
    assert idx == [0]


@pytest.mark.asyncio
async def test_classify_filters_out_of_range_and_dedups():
    facts = ["a", "b"]
    with patch("app.modules.assist_guide.model_router.tool_call",
               new=AsyncMock(return_value=_resp({"superseded_indices": [0, 0, 5, -1]}))):
        idx = await assist_guide.classify_superseded_facts(note_text="reset", facts=facts)
    assert idx == [0]


@pytest.mark.asyncio
async def test_classify_failsoft_on_error():
    with patch("app.modules.assist_guide.model_router.tool_call",
               new=AsyncMock(side_effect=RuntimeError("down"))):
        idx = await assist_guide.classify_superseded_facts(note_text="reset", facts=["a", "b", "c"])
    assert idx == []   # never nuke on a flaky classifier


@pytest.mark.asyncio
async def test_sweep_disabled_is_noop(monkeypatch):
    monkeypatch.setattr(settings, "assist_reset_facts_sweep_enabled", False, raising=False)
    out = await assist_agent.sweep_superseded_facts(
        session_id="s", note_text="reset", db=AsyncMock())
    assert out["retracted"] == []


@pytest.mark.asyncio
async def test_sweep_retracts_via_set_environment(monkeypatch):
    monkeypatch.setattr(settings, "assist_reset_facts_sweep_enabled", True, raising=False)
    monkeypatch.setattr(settings, "assist_reset_facts_sweep_max_frac", 0.9, raising=False)
    facts = ["old VM boot loop", "host 2 NICs", "DeFruscioBridge IP", "new AI-VM created"]
    db = _db_with_facts(facts)
    with patch.object(assist_guide, "classify_superseded_facts",
                      new=AsyncMock(return_value=[0])), \
         patch.object(assist_agent, "set_environment", new=AsyncMock()) as se:
        out = await assist_agent.sweep_superseded_facts(
            session_id="s", note_text="reset — rebuilt clean", db=db)
    assert out["retracted"] == ["old VM boot loop"]
    se.assert_awaited_once()
    assert se.await_args.kwargs["retract_facts"] == ["old VM boot loop"]


@pytest.mark.asyncio
async def test_sweep_overbroad_is_skipped(monkeypatch):
    # A model that wants to retract (nearly) the whole ledger has misfired → skip.
    monkeypatch.setattr(settings, "assist_reset_facts_sweep_enabled", True, raising=False)
    monkeypatch.setattr(settings, "assist_reset_facts_sweep_max_frac", 0.9, raising=False)
    facts = ["a", "b", "c", "d"]  # cap = int(4*0.9) = 3
    db = _db_with_facts(facts)
    with patch.object(assist_guide, "classify_superseded_facts",
                      new=AsyncMock(return_value=[0, 1, 2, 3])), \
         patch.object(assist_agent, "set_environment", new=AsyncMock()) as se:
        out = await assist_agent.sweep_superseded_facts(
            session_id="s", note_text="reset everything", db=db)
    assert out.get("skipped") == "overbroad"
    se.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_too_few_facts_noop(monkeypatch):
    monkeypatch.setattr(settings, "assist_reset_facts_sweep_enabled", True, raising=False)
    db = _db_with_facts(["only one"])
    with patch.object(assist_agent, "set_environment", new=AsyncMock()) as se:
        out = await assist_agent.sweep_superseded_facts(session_id="s", note_text="reset", db=db)
    assert out["retracted"] == []
    se.assert_not_awaited()
