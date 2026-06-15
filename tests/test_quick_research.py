"""Tests for gt_extractor.quick_research — the synchronous grounded-research
primitive (SearXNG search + native-tool-call distill, no autonomous loop).

§17.x — the fast counterpart to the long-running /research SSE loop, used
batched-at-/go by the decomposition fan-out (one call per component).
"""
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

_path = next(
    (p for p in [
        Path(__file__).resolve().parent.parent / "app" / "modules" / "gt_extractor.py",
        Path("/code/app/modules/gt_extractor.py"),
    ] if p.exists()),
    None,
)
if _path is None:
    pytest.skip("gt_extractor.py not found", allow_module_level=True)

spec = importlib.util.spec_from_file_location("gt_extractor_qr", _path)
gt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gt)


@pytest.mark.smoke
class TestQuickResearch:
    async def test_empty_queries_short_circuits(self):
        # No queries -> no search/distill, deterministic empty shape.
        out = await gt.quick_research([])
        assert out == {"entries": [], "results_found": 0, "ingested": 0}

    async def test_searches_distills_and_dedupes_by_url(self):
        gt.search_searxng = AsyncMock(side_effect=[
            [{"title": "A", "url": "http://x/1", "content": "c1"},
             {"title": "B", "url": "http://x/2", "content": "c2"}],
            [{"title": "B2", "url": "http://x/2", "content": "dup"},   # dup URL
             {"title": "C", "url": "http://x/3", "content": "c3"}],
        ])
        captured = {}

        async def fake_distill(results, *, topic, route=None, max_results=15):
            captured["n"] = len(results)
            captured["topic"] = topic
            return [{"title": "f", "content": "fact"}]

        gt.distill_entries = fake_distill
        with patch.object(gt.asyncio, "sleep", AsyncMock()):  # skip inter-query delay
            out = await gt.quick_research(
                ["nasm boot sector", "self-modifying code"], domain="eng",
            )
        assert captured["n"] == 3            # 4 raw results, 1 duplicate URL dropped
        assert captured["topic"] == "nasm boot sector"  # first query is the distill topic
        assert out["results_found"] == 3
        assert out["entries"] == [{"title": "f", "content": "fact"}]
        assert out["ingested"] == 0          # ingest defaults off

    async def test_ingest_off_never_touches_rag(self):
        gt.search_searxng = AsyncMock(
            return_value=[{"title": "A", "url": "http://x/1", "content": "c"}]
        )
        gt.distill_entries = AsyncMock(return_value=[{"title": "f", "content": "fact"}])
        out = await gt.quick_research(["q"], ingest=False)
        assert out["ingested"] == 0
        assert out["entries"] == [{"title": "f", "content": "fact"}]

    async def test_no_results_yields_empty_entries(self):
        gt.search_searxng = AsyncMock(return_value=[])
        gt.distill_entries = AsyncMock(return_value=[])  # nothing to distill
        out = await gt.quick_research(["obscure"], domain="eng")
        assert out["entries"] == []
        assert out["results_found"] == 0
