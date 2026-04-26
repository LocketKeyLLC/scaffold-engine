"""tests/test_rag_pipeline.py - Behavioral tests for RAG pipeline module."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _make_rag_result(**kwargs):
    from app.modules.rag_pipeline import RagResult
    return RagResult(**kwargs)


def _make_vector_results(items):
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


def _patch_rag_deps(
    collection_ok=True, embed_ok=True, vector_results=None,
    keyword_results=None, rerank_passthrough=True,
    superseded_ids=None,
):
    """Build the standard dependency patches for query_rag tests.

    Returns a dict ready to splat into multiple patch() context managers.
    """
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
        """New contract: _rerank returns (ranked, meta)."""
        for r in results:
            r.rerank_score = r.rrf_score
            r.final_score = r.rrf_score
        meta = {"backend": "mock", "skipped_rerank": False, "warnings": []}
        return results[:top_k], meta

    async def mock_superseded(collection, ids):
        return set(superseded_ids or [])

    rerank_mock = (
        AsyncMock(side_effect=mock_rerank)
        if rerank_passthrough
        else AsyncMock(return_value=([], {"backend": None, "skipped_rerank": True, "warnings": []}))
    )

    return {
        "_get_collection": MagicMock(return_value=mock_collection),
        "_embed_query": AsyncMock(return_value=embedding),
        "_vector_search": AsyncMock(return_value=vector_results),
        "_keyword_search": AsyncMock(return_value=keyword_results),
        "_rerank": rerank_mock,
        "_lookup_superseded": AsyncMock(side_effect=mock_superseded),
    }


def _apply_patches(patches):
    """Turn a dict of {attr_name: mock} into a list of patch() context managers."""
    return [patch(f"app.modules.rag_pipeline.{name}", mock) for name, mock in patches.items()]


class _PatchStack:
    def __init__(self, patches):
        self._ctxs = _apply_patches(patches)
    def __enter__(self):
        return [c.__enter__() for c in self._ctxs]
    def __exit__(self, *a):
        for c in reversed(self._ctxs):
            c.__exit__(*a)


# ===========================================================================
# query_rag Happy Path
# ===========================================================================

@pytest.mark.smoke
class TestQueryRagHappyPath:

    def test_returns_dict_with_results(self):
        with _PatchStack(_patch_rag_deps()):
            from app.modules.rag_pipeline import query_rag
            result = _run(query_rag("test query", domain="eng", confidence_threshold=0.0))
        assert isinstance(result, dict)
        assert len(result["results"]) > 0

    def test_result_has_required_fields(self):
        with _PatchStack(_patch_rag_deps()):
            from app.modules.rag_pipeline import query_rag
            result = _run(query_rag("test query", domain="eng", confidence_threshold=0.0))
        r = result["results"][0]
        for field in ["content", "title", "entry_id", "domain", "scores"]:
            assert field in r, f"Missing field: {field}"

    def test_scores_has_vector_keyword_rrf_rerank(self):
        with _PatchStack(_patch_rag_deps()):
            from app.modules.rag_pipeline import query_rag
            result = _run(query_rag("test query", domain="eng", confidence_threshold=0.0))
        scores = result["results"][0]["scores"]
        for key in ["vector", "keyword", "rrf", "rerank", "final"]:
            assert key in scores, f"Missing score: {key}"

    def test_returns_metadata_with_new_fields(self):
        with _PatchStack(_patch_rag_deps()):
            from app.modules.rag_pipeline import query_rag
            result = _run(query_rag("test query", domain="eng", confidence_threshold=0.0))
        md = result["metadata"]
        for key in [
            "reranked", "reranker_backend", "warnings",
            "skipped_rerank", "below_threshold", "fell_back_to_top3",
        ]:
            assert key in md, f"Missing metadata key: {key}"
        assert md["reranker_backend"] == "mock"
        assert md["warnings"] == []


# ===========================================================================
# Error Handling
# ===========================================================================

@pytest.mark.smoke
class TestQueryRagErrors:

    def test_collection_unavailable(self):
        with _PatchStack(_patch_rag_deps(collection_ok=False)):
            from app.modules.rag_pipeline import query_rag
            result = _run(query_rag("test query"))
        assert result["status"] == "error"
        assert "not available" in result["error"]
        assert result["results"] == []

    def test_embedding_failure(self):
        with _PatchStack(_patch_rag_deps(embed_ok=False)):
            from app.modules.rag_pipeline import query_rag
            result = _run(query_rag("test query", domain="eng", confidence_threshold=0.0))
        assert result["status"] == "error"
        assert "embedding" in result["error"].lower()
        assert result["results"] == []


# ===========================================================================
# Domain contract (no silent defaults)
# ===========================================================================

@pytest.mark.smoke
class TestDomainContract:

    def test_domain_none_returns_none_expr(self):
        """_domain_expr(None) returns None; fan-out is the caller's job.

        Milvus partition-key isolation rejects both unfiltered exprs and IN
        exprs over the partition key, so per-partition == exprs are the only
        safe path. _iter_search_domains is what expands None → all domains.
        """
        from app.modules.rag_pipeline import _domain_expr
        assert _domain_expr(None) is None

    def test_iter_search_domains_expands_none_to_all_valid(self):
        from app.modules.rag_pipeline import _iter_search_domains
        from app.config import VALID_DOMAINS
        assert set(_iter_search_domains(None)) == set(VALID_DOMAINS)

    def test_iter_search_domains_passes_through_single_domain(self):
        from app.modules.rag_pipeline import _iter_search_domains
        assert _iter_search_domains("eng") == ["eng"]

    def test_iter_search_domains_rejects_empty(self):
        from app.modules.rag_pipeline import _iter_search_domains
        with pytest.raises(ValueError):
            _iter_search_domains("")

    def test_domain_empty_raises(self):
        from app.modules.rag_pipeline import _domain_expr
        with pytest.raises(ValueError):
            _domain_expr("")

    def test_domain_literal_is_escaped(self):
        from app.modules.rag_pipeline import _domain_expr
        got = _domain_expr('e"ng')
        assert got == 'domain == "e\\"ng"'

    def test_query_rag_passes_none_domain_through(self):
        patches = _patch_rag_deps()
        with _PatchStack(patches):
            from app.modules.rag_pipeline import query_rag
            _run(query_rag("x", domain=None))
        # Both search paths were invoked with domain=None (no partition filter).
        _, vec_kwargs = patches["_vector_search"].call_args
        _, kw_kwargs = patches["_keyword_search"].call_args
        assert vec_kwargs["domain"] is None
        assert kw_kwargs["domain"] is None


# ===========================================================================
# RRF Fusion
# ===========================================================================

@pytest.mark.smoke
class TestRRFFusion:

    def test_fuse_combines_both_sources(self):
        from app.modules.rag_pipeline import RagResult, _rrf_fuse
        vec = [RagResult(content="shared doc", vector_score=0.9, entry_id="e1")]
        kw = [RagResult(content="shared doc", keyword_score=0.8, entry_id="e1")]
        fused = _rrf_fuse(vec, kw)
        assert len(fused) == 1
        assert fused[0].rrf_score > 1.0 / 61

    def test_fuse_does_not_mutate_inputs(self):
        """dataclasses.replace path — input objects must keep rrf_score=0."""
        from app.modules.rag_pipeline import RagResult, _rrf_fuse
        vec_in = [RagResult(content="a", vector_score=0.9, entry_id="e1")]
        kw_in = [RagResult(content="a", keyword_score=0.8, entry_id="e1")]
        _rrf_fuse(vec_in, kw_in)
        assert vec_in[0].rrf_score == 0.0
        assert kw_in[0].rrf_score == 0.0

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
        assert fused[0].content == "doc B"

    def test_fuse_dedup_uses_entry_id_not_content_prefix(self):
        from app.modules.rag_pipeline import RagResult, _rrf_fuse
        vec = [RagResult(content="Foo.  ", vector_score=0.9, entry_id="e1")]
        kw = [RagResult(content="foo", keyword_score=0.8, entry_id="e1")]
        fused = _rrf_fuse(vec, kw)
        assert len(fused) == 1

        shared_prefix = "SHARED BOILERPLATE " * 20
        vec2 = [RagResult(content=shared_prefix + "alpha", vector_score=0.9, entry_id="e1")]
        kw2 = [RagResult(content=shared_prefix + "beta", keyword_score=0.8, entry_id="e2")]
        fused2 = _rrf_fuse(vec2, kw2)
        assert len(fused2) == 2

    def test_fuse_falls_back_to_content_when_entry_id_missing(self):
        from app.modules.rag_pipeline import RagResult, _rrf_fuse
        vec = [RagResult(content="malformed doc", vector_score=0.9, entry_id="")]
        kw = [RagResult(content="malformed doc", keyword_score=0.8, entry_id="")]
        fused = _rrf_fuse(vec, kw)
        assert len(fused) == 1


# ===========================================================================
# Rerank empty-items fallback (new)
# ===========================================================================

@pytest.mark.smoke
class TestRerankEmptyItems:
    """_rerank must warn + fall back to RRF when reranker returns no items."""

    def test_empty_items_triggers_fallback(self):
        from app.modules.rag_pipeline import _rerank, RagResult

        fake_rr = MagicMock()
        fake_rr.items = []            # reranker returned nothing
        fake_rr.backend = "broken"
        fake_rr.latency_ms = 10.0

        results = [
            RagResult(content="a", entry_id="e1", rrf_score=0.5),
            RagResult(content="b", entry_id="e2", rrf_score=0.3),
        ]

        with patch(
            "app.modules.rag_pipeline.cross_encoder_rerank",
            return_value=fake_rr,
        ):
            ranked, meta = _run(_rerank("q", results, top_k=10))

        assert meta["skipped_rerank"] is True
        assert "reranker_returned_no_items" in meta["warnings"]
        assert meta["backend"] == "broken"
        # Fallback: final_score == rrf_score
        assert ranked[0].final_score == 0.5
        assert ranked[1].final_score == 0.3


# ===========================================================================
# Confidence Threshold Relaxation
# ===========================================================================

@pytest.mark.smoke
class TestConfidenceThreshold:

    def test_too_strict_fallback_returns_results_and_flags_metadata(self):
        low = _make_vector_results([
            {"content": "Low A", "entry_id": "e1", "vector_score": 0.3},
            {"content": "Low B", "entry_id": "e2", "vector_score": 0.2},
            {"content": "Low C", "entry_id": "e3", "vector_score": 0.1},
        ])

        async def low_rerank(query, results, top_k):
            for r in results:
                r.final_score = 0.1  # below default 0.8
            meta = {"backend": "mock", "skipped_rerank": False, "warnings": []}
            return results[:top_k], meta

        patches = _patch_rag_deps(vector_results=low)
        patches["_rerank"] = AsyncMock(side_effect=low_rerank)

        with _PatchStack(patches):
            from app.modules.rag_pipeline import query_rag
            result = _run(query_rag("obscure query", domain="eng"))

        assert 0 < len(result["results"]) <= 3
        md = result["metadata"]
        assert md["below_threshold"] is True
        assert md["fell_back_to_top3"] is True
        assert "below_threshold" in md["warnings"]
        assert "fell_back_to_top3" in md["warnings"]


# ===========================================================================
# Version Filtering — now uses post-query Milvus supersedes lookup
# ===========================================================================

@pytest.mark.smoke
class TestVersionFiltering:

    def test_superseded_entry_removed_via_milvus_lookup(self):
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
        # Milvus-side lookup says: "e1 is superseded by some newer row."
        patches = _patch_rag_deps(vector_results=results, superseded_ids={"e1"})

        with _PatchStack(patches):
            from app.modules.rag_pipeline import query_rag
            result = _run(query_rag("test", domain="eng"))
        entry_ids = [r["entry_id"] for r in result["results"]]
        assert "e1" not in entry_ids
        assert "e2" in entry_ids

    def test_include_history_skips_lookup(self):
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
        patches = _patch_rag_deps(vector_results=results, superseded_ids={"e1"})

        with _PatchStack(patches):
            from app.modules.rag_pipeline import query_rag
            result = _run(query_rag("test", domain="eng", include_history=True))
        entry_ids = [r["entry_id"] for r in result["results"]]
        assert "e1" in entry_ids
        assert "e2" in entry_ids
        # include_history bypasses the lookup entirely
        patches["_lookup_superseded"].assert_not_awaited()


# ===========================================================================
# Ingest — upsert, batch embed, empty domain
# ===========================================================================

@pytest.mark.smoke
class TestIngestContract:

    def test_ingest_uses_upsert_not_insert(self):
        import app.modules.rag_pipeline as rp

        fake_col = MagicMock()
        fake_col.query = MagicMock(return_value=[])
        fake_col.search = MagicMock(return_value=[[]])
        fake_col.upsert = MagicMock()
        fake_col.insert = MagicMock()
        fake_col.flush = MagicMock()

        async def fake_batch(texts):
            return [[0.0] * 512 for _ in texts]

        with patch.object(rp, "_get_collection", MagicMock(return_value=fake_col)), \
             patch.object(rp, "_embed_contents_batch", AsyncMock(side_effect=fake_batch)):
            _run(rp.ingest_entries(
                [{"title": "t", "content": "hello world", "tags": []}],
                domain="eng",
            ))

        fake_col.upsert.assert_called_once()
        fake_col.insert.assert_not_called()

    def test_ingest_rejects_empty_domain(self):
        import app.modules.rag_pipeline as rp
        with pytest.raises(ValueError):
            _run(rp.ingest_entries([{"title": "t", "content": "x"}], domain=""))
