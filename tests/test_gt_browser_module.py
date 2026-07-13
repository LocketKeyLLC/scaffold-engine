"""Smoke tests for app/modules/gt_browser.py (Phase 6 audit fixes).

Covers:
    #6.4  — gt_detail raises HTTPException(404) on missing entry
    #6.19 — gt_browser imports embed_query from public utils, not _embed_query
    #6.20 — gt_list / gt_search filter superseded entries by default
    #6.21 — gt_stats returns truncated=True when scan budget exhausted

Separate from tests/test_gt_browser.py which covers the *pipeline* layer.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.modules import gt_browser


@pytest.mark.smoke
def test_imports_public_embed_query():
    import inspect
    src = inspect.getsource(gt_browser)
    assert "from app.utils.embedding import embed_query" in src
    assert "from app.modules.rag_pipeline import _embed_query" not in src


@pytest.mark.smoke
class TestGtDetail404:
    @pytest.mark.asyncio
    async def test_missing_entry_raises_404(self):
        fake_col = MagicMock()
        fake_col.query.return_value = []
        with patch.object(gt_browser, "_get_client", return_value=fake_col):
            with pytest.raises(HTTPException) as exc:
                await gt_browser.gt_detail("nonexistent-id")
        assert exc.value.status_code == 404
        assert "nonexistent-id" in str(exc.value.detail)

    @pytest.mark.asyncio
    async def test_existing_entry_returns_payload(self):
        fake_col = MagicMock()
        fake_col.query.return_value = [{
            "entry_id": "e1", "title": "T", "domain": "rag",
            "domain_tags": ["t1"], "canonical_text": "body",
            "source_url": "", "confidence_score": 0.9,
            "source_type": "tech_docs", "supersedes_id": "",
        }]
        with patch.object(gt_browser, "_get_client", return_value=fake_col):
            result = await gt_browser.gt_detail("e1")
        assert result["found"] is True
        assert result["entry_id"] == "e1"


@pytest.mark.smoke
class TestSupersedeFilter:
    @pytest.mark.asyncio
    async def test_gt_list_default_hides_superseded(self):
        fake_col = MagicMock()
        fake_col.num_entities = 2
        fake_col.query.return_value = []
        with patch.object(gt_browser, "_get_client", return_value=fake_col):
            await gt_browser.gt_list(page=1, per_page=20)
        expr = fake_col.query.call_args.kwargs["filter"]
        assert 'supersedes_id == ""' in expr

    @pytest.mark.asyncio
    async def test_gt_list_include_history_no_filter(self):
        fake_col = MagicMock()
        fake_col.num_entities = 2
        fake_col.query.return_value = []
        with patch.object(gt_browser, "_get_client", return_value=fake_col):
            await gt_browser.gt_list(page=1, per_page=20, include_history=True)
        expr = fake_col.query.call_args.kwargs["filter"]
        assert "supersedes_id" not in expr

    @pytest.mark.asyncio
    async def test_gt_search_default_hides_superseded(self):
        fake_col = MagicMock()
        fake_col.search.return_value = [[]]
        with patch.object(gt_browser, "_get_client", return_value=fake_col), \
             patch.object(gt_browser, "embed_query", AsyncMock(return_value=[0.1] * 512)):
            await gt_browser.gt_search(query="x", top_k=5)
        expr = fake_col.search.call_args.kwargs["filter"]
        assert expr is not None and 'supersedes_id == ""' in expr


@pytest.mark.smoke
class TestStatsTruncation:
    @pytest.mark.asyncio
    async def test_stats_reports_truncated(self):
        fake_col = MagicMock()
        page = [
            {"title": f"t{i}", "domain": "rag", "domain_tags": ["x"], "source_type": "tech_docs"}
            for i in range(16384)
        ]
        fake_col.query.return_value = page
        with patch.object(gt_browser, "_get_client", return_value=fake_col), \
             patch.object(gt_browser, "_count_entries", return_value=500_000), \
             patch.object(gt_browser.settings, "gt_stats_scan_limit", 16384):
            result = await gt_browser.gt_stats()
        assert result["truncated"] is True
        assert result["scanned"] == 16384
        assert result["total_entries"] == 500_000

    @pytest.mark.asyncio
    async def test_stats_not_truncated_when_all_scanned(self):
        fake_col = MagicMock()
        fake_col.query.return_value = [
            {"title": "t", "domain": "rag", "domain_tags": ["x"], "source_type": "tech_docs"}
        ] * 100
        with patch.object(gt_browser, "_get_client", return_value=fake_col), \
             patch.object(gt_browser, "_count_entries", return_value=100):
            result = await gt_browser.gt_stats()
        assert result["truncated"] is False
        assert result["scanned"] == 100
