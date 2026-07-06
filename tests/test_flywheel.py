"""§17.576 — learning flywheel (maybe_ingest_exemplar / retrieve_exemplars).

Opt-in, default-OFF, fail-soft. Patches the lazily-imported ingest_entries /
query_rag at their source module (app.modules.rag_pipeline).
"""
from unittest.mock import AsyncMock

import pytest

import app.modules.rag_pipeline as rp
from app.config import settings
from app.modules import flywheel


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.setattr(settings, "exemplar_min_grounding", 0.85)
    monkeypatch.setattr(settings, "exemplar_retrieval_top_k", 2)


# ---- ingestion ----

@pytest.mark.asyncio
async def test_ingest_disabled_noop(monkeypatch):
    monkeypatch.setattr(settings, "exemplar_ingest_enabled", False)
    ie = AsyncMock()
    monkeypatch.setattr(rp, "ingest_entries", ie)
    out = await flywheel.maybe_ingest_exemplar(
        job_id="j", compiled_output="x", deliverable_kind="report", grounding_score=0.99)
    assert out is False
    ie.assert_not_awaited()


@pytest.mark.asyncio
async def test_ingest_below_threshold_noop(monkeypatch):
    monkeypatch.setattr(settings, "exemplar_ingest_enabled", True)
    ie = AsyncMock()
    monkeypatch.setattr(rp, "ingest_entries", ie)
    out = await flywheel.maybe_ingest_exemplar(
        job_id="j", compiled_output="x", deliverable_kind="report", grounding_score=0.5)
    assert out is False
    ie.assert_not_awaited()


@pytest.mark.asyncio
async def test_ingest_plan_only_skipped(monkeypatch):
    monkeypatch.setattr(settings, "exemplar_ingest_enabled", True)
    ie = AsyncMock()
    monkeypatch.setattr(rp, "ingest_entries", ie)
    out = await flywheel.maybe_ingest_exemplar(
        job_id="j", compiled_output="x", deliverable_kind="plan_only", grounding_score=0.99)
    assert out is False
    ie.assert_not_awaited()


@pytest.mark.asyncio
async def test_ingest_high_grounding_tags_exemplar(monkeypatch):
    monkeypatch.setattr(settings, "exemplar_ingest_enabled", True)
    ie = AsyncMock()
    monkeypatch.setattr(rp, "ingest_entries", ie)
    out = await flywheel.maybe_ingest_exemplar(
        job_id="job1234ab", compiled_output="the deliverable",
        deliverable_kind="report", grounding_score=0.92)
    assert out is True
    ie.assert_awaited_once()
    entries = ie.call_args.args[0]
    assert entries[0]["source_type"] == "exemplar"
    assert entries[0]["confidence_score"] == 0.92
    assert "exemplar" in entries[0]["domain_tags"]


# ---- retrieval ----

@pytest.mark.asyncio
async def test_retrieve_disabled_noop(monkeypatch):
    monkeypatch.setattr(settings, "exemplar_retrieval_enabled", False)
    qr = AsyncMock()
    monkeypatch.setattr(rp, "query_rag", qr)
    assert await flywheel.retrieve_exemplars("q") == ""
    qr.assert_not_awaited()


@pytest.mark.asyncio
async def test_retrieve_filters_to_exemplars(monkeypatch):
    monkeypatch.setattr(settings, "exemplar_retrieval_enabled", True)
    resp = {"status": "ok", "results": [
        {"source_type": "web", "content": "not an exemplar"},
        {"source_type": "exemplar", "content": "proven solution A", "confidence_score": 0.9},
        {"source_type": "exemplar", "content": "proven solution B", "confidence_score": 0.88},
    ]}
    monkeypatch.setattr(rp, "query_rag", AsyncMock(return_value=resp))
    out = await flywheel.retrieve_exemplars("build a parser", domain="eng")
    assert "Proven prior solutions" in out
    assert "proven solution A" in out and "proven solution B" in out
    assert "not an exemplar" not in out          # post-filter drops non-exemplars


@pytest.mark.asyncio
async def test_retrieve_none_found(monkeypatch):
    monkeypatch.setattr(settings, "exemplar_retrieval_enabled", True)
    monkeypatch.setattr(rp, "query_rag", AsyncMock(
        return_value={"status": "ok", "results": [{"source_type": "web", "content": "x"}]}))
    assert await flywheel.retrieve_exemplars("q") == ""
