"""§17.757 — cross-component fact sharing: the funnel folds facts observed on
sibling components of the same umbrella into a later component's grounding.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.modules import assist_agent


def _scalar_result(value):
    r = MagicMock()
    r.scalar.return_value = value
    return r


def _rows_result(metadatas):
    r = MagicMock()
    r.mappings.return_value.all.return_value = [{"metadata": m} for m in metadatas]
    return r


@pytest.mark.asyncio
async def test_disabled_returns_empty(monkeypatch):
    monkeypatch.setattr(settings, "assist_cross_component_facts_enabled", False, raising=False)
    assert await assist_agent._sibling_facts(job_id="j", db=AsyncMock()) == []


@pytest.mark.asyncio
async def test_standalone_job_no_parent(monkeypatch):
    monkeypatch.setattr(settings, "assist_cross_component_facts_enabled", True, raising=False)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_scalar_result(None))   # no parent_job_id
    assert await assist_agent._sibling_facts(job_id="j", db=db) == []


@pytest.mark.asyncio
async def test_collects_and_dedups_sibling_facts(monkeypatch):
    monkeypatch.setattr(settings, "assist_cross_component_facts_enabled", True, raising=False)
    monkeypatch.setattr(settings, "assist_cross_component_facts_cap", 40, raising=False)
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result("umbrella-1"),                          # parent lookup
        _rows_result([
            {"environment": {"facts": ["host has 2 NICs", "DeFruscioBridge is 10.10.10.1/24"]}},
            {"environment": {"facts": ["host has 2 NICs", "ZFS pool 'tank' exists"]}},  # dup dropped
        ]),
    ])
    out = await assist_agent._sibling_facts(job_id="me", db=db)
    assert out == ["host has 2 NICs", "DeFruscioBridge is 10.10.10.1/24", "ZFS pool 'tank' exists"]


@pytest.mark.asyncio
async def test_cap_is_respected(monkeypatch):
    monkeypatch.setattr(settings, "assist_cross_component_facts_enabled", True, raising=False)
    monkeypatch.setattr(settings, "assist_cross_component_facts_cap", 2, raising=False)
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _scalar_result("umbrella-1"),
        _rows_result([{"environment": {"facts": ["a", "b", "c", "d"]}}]),
    ])
    out = await assist_agent._sibling_facts(job_id="me", db=db)
    assert out == ["a", "b"]


@pytest.mark.asyncio
async def test_funnel_merges_sibling_facts_own_first(monkeypatch):
    monkeypatch.setattr(settings, "assist_cross_component_facts_enabled", True, raising=False)
    monkeypatch.setattr(settings, "assist_job_context_enabled", False, raising=False)
    monkeypatch.setattr(settings, "assist_step_recap_enabled", False, raising=False)
    monkeypatch.setattr(settings, "assist_status_panel_enabled", False, raising=False)
    monkeypatch.setattr(settings, "assist_project_recap_enabled", False, raising=False)
    sess = {"job_id": "me", "metadata": {"environment": {"facts": ["my own fact"]}}, "notes": []}
    with_patches = pytest.MonkeyPatch()
    with_patches.setattr(assist_agent, "_sibling_facts",
                         AsyncMock(return_value=["shared host fact", "my own fact"]))  # last is a dup
    with_patches.setattr(assist_agent, "_job_digest_for", AsyncMock(return_value=""))
    with_patches.setattr(assist_agent, "get_project_recap", AsyncMock(return_value=""))
    with_patches.setattr(assist_agent, "get_step_recap", AsyncMock(return_value=""))
    with_patches.setattr(assist_agent, "_history_or_transcript", AsyncMock(return_value=[]))
    try:
        mem = await assist_agent.assemble_generation_memory(
            session_id="s", nk="T1", sess=sess, db=AsyncMock(), digest_excludes=set())
    finally:
        with_patches.undo()
    facts = mem.environment.get("facts")
    assert facts == ["my own fact", "shared host fact"]   # own first, dup not repeated
