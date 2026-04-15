"""
tests/test_rag_pipeline.py - Behavioral tests for RAG pipeline module

Tests query_rag() and _rrf_fuse() by mocking Milvus collection,
embedding, and reranker dependencies.

Run:  docker exec scaffold-orchestrator pytest tests/test_rag_pipeline.py -m smoke --timeout=30 -v
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _make_rag_result(**kwargs):
    """Build a RagResult using the real dataclass."""
    from app.modules.rag_pipeline import RagResult
    return RagResult(**kwargs)


def _make_vector_results(items):
    """Build a list of RagResult from dicts for vector search mock."""
    results = []
    for item in items:
        results.append(_make_rag_result(
            content=item.get("content", "content"),
            title=item.get("title", "Title"),
            entry_id=item.get("entry_id", "e1"),
            domain=item.get("domain", "eng"),
            vector_score=item.get("vector_score", 0.9),
            version=item.get("version", 1),
            supersedes_id=item.get("supersedes_id", ""),
        ))
    return results


def _patch_rag_deps(collection_ok=True, embed_ok=True, vector_results=None,
                    keyword_results=None, rerank_passthrough=True):
    """Return a dict of patches for query_rag dependencies."""
    mock_collection = MagicMock() if collection_ok else None

    embedding = [0.1] * 512 if embed_ok else None

    if vector_results is None:
        vector_results = _make_vector_results([
            {"content": "Result A", "title": "Doc A", "entry_id": "e1",
             "vector_score": 0.95},
        ])
    if keyword_results is None:
        keyword_results = []

    async def mock_rerank(query, results, top_k):
        """Pass through with final_score = rerank_score or rrf_score."""
        for r in results:
            r.rerank_score = r.rrf_score
            r.final_score = r.rrf_score
        return results[:top_k]

    return {
        "app.modules.rag_pipeline._get_collection": MagicMock(
            return_value=mock_collection),
        "app.modules.rag_pipeline._embed_query": AsyncMock(
            return_value=embedding),
        "app.modules.rag_pipeline._vector_search": AsyncMock(
            return_value=vector_results),
        "app.modules.rag_pipeline._keyword_search": AsyncMock(
            return_value=keyword_results),
        "app.modules.rag_pipeline._rerank": AsyncMock(
            side_effect=mock_rerank) if rerank_passthrough else AsyncMock(
            return_value=[]),
    }


# ===========================================================================
# query_rag Happy Path
# ===========================================================================

@pytest.mark.smoke
class TestQueryRagHappyPath:
    """query_rag() returns structured results on success."""

    def test_returns_dict_with_results(self):
        patches = _patch_rag_deps()
        with patch.multiple("", **patches, create=False) if False else \
             patch("app.modules.rag_pipeline._get_collection", patches["app.modules.rag_pipeline._get_collection"]), \
             patch("app.modules.rag_pipeline._embed_query", patches["app.modules.rag_pipeline._embed_query"]), \
             patch("app.modules.rag_pipeline._vector_search", patches["app.modules.rag_pipeline._vector_search"]), \
             patch("app.modules.rag_pipeline._keyword_search", patches["app.modules.rag_pipeline._keyword_search"]), \
             patch("app.modules.rag_pipeline._rerank", patches["app.modules.rag_pipeline._rerank"]):
            from app.modules.rag_pipeline import query_rag
            result = _run(query_rag("test query", domain="eng"))
        assert isinstance(result, dict)
        assert "results" in result
        assert len(result["results"]) > 0

    def test_result_has_required_fields(self):
        patches = _patch_rag_deps()
        with patch("app.modules.rag_pipeline._get_collection", patches["app.modules.rag_pipeline._get_collection"]), \
             patch("app.modules.rag_pipeline._embed_query", patches["app.modules.rag_pipeline._embed_query"]), \
             patch("app.modules.rag_pipeline._vector_search", patches["app.modules.rag_pipeline._vector_search"]), \
             patch("app.modules.rag_pipeline._keyword_search", patches["app.modules.rag_pipeline._keyword_search"]), \
             patch("app.modules.rag_pipeline._rerank", patches["app.modules.rag_pipeline._rerank"]):
            from app.modules.rag_pipeline import query_rag
            result = _run(query_rag("test query", domain="eng"))
        r = result["results"][0]
        for field in ["content", "title", "entry_id", "domain", "scores"]:
            assert field in r, f"Missing field: {field}"

    def test_scores_has_vector_keyword_rrf_rerank(self):
        patches = _patch_rag_deps()
        with patch("app.modules.rag_pipeline._get_collection", patches["app.modules.rag_pipeline._get_collection"]), \
             patch("app.modules.rag_pipeline._embed_query", patches["app.modules.rag_pipeline._embed_query"]), \
             patch("app.modules.rag_pipeline._vector_search", patches["app.modules.rag_pipeline._vector_search"]), \
             patch("app.modules.rag_pipeline._keyword_search", patches["app.modules.rag_pipeline._keyword_search"]), \
             patch("app.modules.rag_pipeline._rerank", patches["app.modules.rag_pipeline._rerank"]):
            from app.modules.rag_pipeline import query_rag
            result = _run(query_rag("test query", domain="eng"))
        scores = result["results"][0]["scores"]
        for key in ["vector", "keyword", "rrf", "rerank", "final"]:
            assert key in scores, f"Missing score: {key}"

    def test_returns_metadata(self):
        patches = _patch_rag_deps()
        with patch("app.modules.rag_pipeline._get_collection", patches["app.modules.rag_pipeline._get_collection"]), \
             patch("app.modules.rag_pipeline._embed_query", patches["app.modules.rag_pipeline._embed_query"]), \
             patch("app.modules.rag_pipeline._vector_search", patches["app.modules.rag_pipeline._vector_search"]), \
             patch("app.modules.rag_pipeline._keyword_search", patches["app.modules.rag_pipeline._keyword_search"]), \
             patch("app.modules.rag_pipeline._rerank", patches["app.modules.rag_pipeline._rerank"]):
            from app.modules.rag_pipeline import query_rag
            result = _run(query_rag("test query", domain="eng"))
        assert "metadata" in result
        assert "reranked" in result["metadata"]


# ===========================================================================
# Error Handling
# ===========================================================================

@pytest.mark.smoke
class TestQueryRagErrors:
    """query_rag() returns error dicts on failure."""

    def test_collection_unavailable(self):
        patches = _patch_rag_deps(collection_ok=False)
        with patch("app.modules.rag_pipeline._get_collection", patches["app.modules.rag_pipeline._get_collection"]), \
             patch("app.modules.rag_pipeline._embed_query", patches["app.modules.rag_pipeline._embed_query"]):
            from app.modules.rag_pipeline import query_rag
            result = _run(query_rag("test query"))
        assert result["status"] == "error"
        assert "not available" in result["error"]
        assert result["results"] == []

    def test_embedding_failure(self):
        patches = _patch_rag_deps(embed_ok=False)
        with patch("app.modules.rag_pipeline._get_collection", patches["app.modules.rag_pipeline._get_collection"]), \
             patch("app.modules.rag_pipeline._embed_query", patches["app.modules.rag_pipeline._embed_query"]):
            from app.modules.rag_pipeline import query_rag
            result = _run(query_rag("test query", domain="eng"))
        assert result["status"] == "error"
        assert "embedding" in result["error"].lower()
        assert result["results"] == []


# ===========================================================================
# RRF Fusion
# ===========================================================================

@pytest.mark.smoke
class TestRRFFusion:
    """_rrf_fuse() merges vector and keyword results correctly."""

    def test_fuse_combines_both_sources(self):
        from app.modules.rag_pipeline import RagResult, _rrf_fuse
        vec = [RagResult(content="shared doc", vector_score=0.9, entry_id="e1")]
        kw = [RagResult(content="shared doc", keyword_score=0.8, entry_id="e1")]
        fused = _rrf_fuse(vec, kw)
        assert len(fused) == 1
        # RRF score should be sum of both contributions
        assert fused[0].rrf_score > 1.0 / 61  # more than single-source contribution

    def test_fuse_preserves_disjoint_results(self):
        from app.modules.rag_pipeline import RagResult, _rrf_fuse
        vec = [RagResult(content="doc A", vector_score=0.9, entry_id="e1")]
        kw = [RagResult(content="doc B", keyword_score=0.8, entry_id="e2")]
        fused = _rrf_fuse(vec, kw)
        assert len(fused) == 2

    def test_fuse_sorted_by_rrf_score_desc(self):
        from app.modules.rag_pipeline import RagResult, _rrf_fuse
        vec = [
            RagResult(content="doc A", vector_score=0.9, entry_id="e1"),
            RagResult(content="doc B", vector_score=0.7, entry_id="e2"),
        ]
        kw = [RagResult(content="doc B", keyword_score=0.95, entry_id="e2")]
        fused = _rrf_fuse(vec, kw)
        # doc B appears in both sources, should rank higher
        assert fused[0].content == "doc B"


# ===========================================================================
# Confidence Threshold Relaxation (too_strict fallback)
# ===========================================================================

@pytest.mark.smoke
class TestConfidenceThreshold:
    """When all results are below threshold, top 3 are returned anyway."""

    def test_too_strict_fallback_returns_results(self):
        low_score_results = _make_vector_results([
            {"content": "Low A", "entry_id": "e1", "vector_score": 0.3},
            {"content": "Low B", "entry_id": "e2", "vector_score": 0.2},
            {"content": "Low C", "entry_id": "e3", "vector_score": 0.1},
        ])
        patches = _patch_rag_deps(vector_results=low_score_results)

        # Override rerank to keep low scores
        async def low_rerank(query, results, top_k):
            for r in results:
                r.final_score = 0.1  # below default threshold of 0.8
            return results[:top_k]

        with patch("app.modules.rag_pipeline._get_collection", patches["app.modules.rag_pipeline._get_collection"]), \
             patch("app.modules.rag_pipeline._embed_query", patches["app.modules.rag_pipeline._embed_query"]), \
             patch("app.modules.rag_pipeline._vector_search", patches["app.modules.rag_pipeline._vector_search"]), \
             patch("app.modules.rag_pipeline._keyword_search", patches["app.modules.rag_pipeline._keyword_search"]), \
             patch("app.modules.rag_pipeline._rerank", AsyncMock(side_effect=low_rerank)):
            from app.modules.rag_pipeline import query_rag
            result = _run(query_rag("obscure query", domain="eng"))
        # Should still return up to 3 results despite being below threshold
        assert len(result["results"]) > 0
        assert len(result["results"]) <= 3


# ===========================================================================
# Version Filtering
# ===========================================================================

@pytest.mark.smoke
class TestVersionFiltering:
    """query_rag() filters superseded entries by default."""

    def test_superseded_entry_removed(self):
        results = [
            _make_rag_result(
                content="Old version", title="Doc", entry_id="e1",
                domain="eng", vector_score=0.9, version=1, supersedes_id="",
            ),
            _make_rag_result(
                content="New version", title="Doc v2", entry_id="e2",
                domain="eng", vector_score=0.95, version=2, supersedes_id="e1",
            ),
        ]
        patches = _patch_rag_deps(vector_results=results)
        with patch("app.modules.rag_pipeline._get_collection", patches["app.modules.rag_pipeline._get_collection"]), \
             patch("app.modules.rag_pipeline._embed_query", patches["app.modules.rag_pipeline._embed_query"]), \
             patch("app.modules.rag_pipeline._vector_search", patches["app.modules.rag_pipeline._vector_search"]), \
             patch("app.modules.rag_pipeline._keyword_search", patches["app.modules.rag_pipeline._keyword_search"]), \
             patch("app.modules.rag_pipeline._rerank", patches["app.modules.rag_pipeline._rerank"]):
            from app.modules.rag_pipeline import query_rag
            result = _run(query_rag("test", domain="eng"))
        entry_ids = [r["entry_id"] for r in result["results"]]
        assert "e1" not in entry_ids, "Superseded entry should be filtered out"
        assert "e2" in entry_ids

    def test_include_history_keeps_all(self):
        results = [
            _make_rag_result(
                content="Old version", title="Doc", entry_id="e1",
                domain="eng", vector_score=0.9, version=1, supersedes_id="",
            ),
            _make_rag_result(
                content="New version", title="Doc v2", entry_id="e2",
                domain="eng", vector_score=0.95, version=2, supersedes_id="e1",
            ),
        ]
        patches = _patch_rag_deps(vector_results=results)
        with patch("app.modules.rag_pipeline._get_collection", patches["app.modules.rag_pipeline._get_collection"]), \
             patch("app.modules.rag_pipeline._embed_query", patches["app.modules.rag_pipeline._embed_query"]), \
             patch("app.modules.rag_pipeline._vector_search", patches["app.modules.rag_pipeline._vector_search"]), \
             patch("app.modules.rag_pipeline._keyword_search", patches["app.modules.rag_pipeline._keyword_search"]), \
             patch("app.modules.rag_pipeline._rerank", patches["app.modules.rag_pipeline._rerank"]):
            from app.modules.rag_pipeline import query_rag
            result = _run(query_rag("test", domain="eng", include_history=True))
        entry_ids = [r["entry_id"] for r in result["results"]]
        assert "e1" in entry_ids
        assert "e2" in entry_ids
