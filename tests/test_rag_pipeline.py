"""tests/test_rag_pipeline.py - Behavioral tests for RAG pipeline module."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _run(coro):
    # Create + close explicitly. A leaked loop produces the
    # `PytestUnraisableExceptionWarning: Invalid file descriptor: -1`
    # benign-but-noisy teardown warning during full-suite runs.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


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

    async def mock_rerank(query, results, top_k, *, max_candidates=None, doc_truncate=None):
        """New contract: _rerank returns (ranked, meta).

        §17.234 — max_candidates kwarg added; mock accepts and ignores
        (mock returns the input results in their existing order; the
        kwarg only matters at the real CrossEncoder boundary).
        §17.252 — doc_truncate kwarg added; same accept-and-ignore.
        """
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

    # Mock async_session so query_rag's provenance batch-fetch doesn't try
    # to hit a real Postgres from inside the test's event loop.
    mock_result = MagicMock()
    mock_result.mappings.return_value = []
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    return {
        "_get_client": MagicMock(return_value=mock_collection),
        "_embed_query": AsyncMock(return_value=embedding),
        "_vector_search": AsyncMock(return_value=vector_results),
        "_keyword_search": AsyncMock(return_value=keyword_results),
        "_rerank": rerank_mock,
        "_lookup_superseded": AsyncMock(side_effect=mock_superseded),
        "async_session": MagicMock(return_value=mock_session),
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
            # §17.253 — effective reranker knobs surfaced in metadata
            "rerank_max_candidates", "rerank_doc_truncate",
        ]:
            assert key in md, f"Missing metadata key: {key}"
        assert md["reranker_backend"] == "mock"
        assert md["warnings"] == []
        # §17.253 — metadata values are resolved ints (not None even when
        # no override was passed)
        assert isinstance(md["rerank_max_candidates"], int)
        assert isinstance(md["rerank_doc_truncate"], int)


# ===========================================================================
# §17.197 — quality-bump no-mutation invariant
# ===========================================================================
#
# Pre-§17.197 the quality-bump phase did ``r.final_score *= bump`` — an
# in-place mutation that broke the no-mutation invariant ``_rerank`` /
# ``_rrf_fuse`` establish via ``dataclasses.replace``. The mutation was
# practically safe because ``filtered`` doesn't escape ``query_rag``,
# but a future change that caches the RagResult list rather than the
# response dict would double-apply the bump on a cache hit. §17.197
# replaces the mutation with ``dataclasses.replace``; the test below
# locks the invariant.

@pytest.mark.smoke
class TestQualityBumpNoMutation:
    def test_quality_bump_phase_uses_replace_not_inplace(self):
        """Source-level guard against re-introducing the in-place
        mutation §17.197 removed. The pre-§17.197 pattern was
        ``r.final_score = r.final_score * bump`` — broke the no-mutation
        invariant ``_rerank`` / ``_rrf_fuse`` establish via
        ``dataclasses.replace``. The current implementation builds a
        new list via ``replace``; this test ensures a future refactor
        can't silently slide back."""
        import inspect
        from app.modules import rag_pipeline as rp
        src = inspect.getsource(rp.query_rag)
        # The forbidden pattern (with optional spaces). Catches the
        # canonical form; a creative refactor using a different variable
        # name would slip past, but the §17.120 quality-bump pattern is
        # stable across the codebase.
        assert "r.final_score = r.final_score" not in src, (
            "regression: quality-bump phase reintroduced in-place "
            "mutation that §17.197 removed"
        )
        # And the replace pattern IS present.
        assert "replace(r, final_score=" in src, (
            "expected `replace(r, final_score=...)` in query_rag for "
            "the §17.197 quality-bump phase"
        )

    def test_bump_factor_applied_to_response_score(self):
        """When quality_bump returns 1.5, the response's final score is
        1.5× the rrf_score (the post-rerank score; mock_rerank sets it).
        Confirms the §17.197 ``replace(r, final_score=r.final_score *
        bump)`` formula actually applies the bump rather than dropping it."""
        vector_results = _make_vector_results([
            {"content": "A", "title": "DA", "entry_id": "e1", "vector_score": 0.9},
        ])
        patches = _patch_rag_deps(vector_results=vector_results)
        with _PatchStack(patches), \
             patch("app.modules.quality_rerank.quality_bump", return_value=1.5):
            from app.modules.rag_pipeline import query_rag
            result = _run(query_rag(
                "test query", domain="eng", confidence_threshold=0.0,
            ))

        final = result["results"][0]["scores"]["final"]
        rrf = result["results"][0]["scores"]["rrf"]
        # mock_rerank sets rerank_score = rrf_score and final_score =
        # rrf_score on its returned list (the new copies built by
        # _rrf_fuse's replace). The bump multiplies that by 1.5.
        assert final == pytest.approx(rrf * 1.5, rel=1e-4)
        assert result["results"][0]["scores"]["quality_bump"] == pytest.approx(1.5)

    def test_bump_of_one_leaves_score_at_rerank_value(self):
        """When quality_bump returns 1.0 (no provenance row → identity),
        the final_score in the response equals the post-rerank score —
        no spurious round-trip changes."""
        vector_results = _make_vector_results([
            {"content": "A", "title": "DA", "entry_id": "e1", "vector_score": 0.9},
        ])
        patches = _patch_rag_deps(vector_results=vector_results)
        with _PatchStack(patches), \
             patch("app.modules.quality_rerank.quality_bump", return_value=1.0):
            from app.modules.rag_pipeline import query_rag
            result = _run(query_rag(
                "test query", domain="eng", confidence_threshold=0.0,
            ))
        final = result["results"][0]["scores"]["final"]
        rrf = result["results"][0]["scores"]["rrf"]
        # Bump factor 1.0 — response score equals the rerank-stage score.
        assert final == pytest.approx(rrf, rel=1e-9)
        assert result["results"][0]["scores"]["quality_bump"] == pytest.approx(1.0)


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


# ===========================================================================
# §17.188 — domain_hint narrows fan-out from N partitions to {hint, "llm"}
# ===========================================================================

@pytest.mark.smoke
class TestDomainHintFanOut:
    """``_iter_search_domains(domain=None, hint=X)`` narrows the all-
    partition fan-out to ``{X, "llm"}`` when X is a valid domain. A strict
    ``domain`` arg always wins (hint is no-op when domain is set). An
    invalid hint logs + falls through to the full fan-out — never raises."""

    def test_hint_with_strict_domain_is_ignored(self):
        """domain="eng" → strict [eng], regardless of hint."""
        from app.modules.rag_pipeline import _iter_search_domains
        assert _iter_search_domains("eng", hint="rag") == ["eng"]

    def test_hint_eng_collapses_fan_out_to_eng_plus_llm(self):
        from app.modules.rag_pipeline import _iter_search_domains
        assert _iter_search_domains(None, hint="eng") == ["eng", "llm"]

    def test_hint_llm_collapses_fan_out_to_just_llm(self):
        """When the hint IS 'llm', the set {hint, 'llm'} = {'llm'} —
        no duplicate search across the same partition."""
        from app.modules.rag_pipeline import _iter_search_domains
        assert _iter_search_domains(None, hint="llm") == ["llm"]

    @pytest.mark.parametrize("hint", ["prompt", "rag", "code", "qa", "spec"])
    def test_every_valid_hint_always_includes_llm_fallback(self, hint):
        """The 'llm' partition is the generic-knowledge fallback included
        on every hint path so cross-domain hits still surface."""
        from app.modules.rag_pipeline import _iter_search_domains
        out = _iter_search_domains(None, hint=hint)
        assert "llm" in out
        assert hint in out
        assert len(out) == 2 if hint != "llm" else 1

    def test_invalid_hint_falls_through_to_full_fan_out(self, caplog):
        """A typo'd hint must not break retrieval — log + fall back to all."""
        import logging
        from app.modules.rag_pipeline import _iter_search_domains
        from app.config import VALID_DOMAINS
        with caplog.at_level(logging.WARNING, logger="scaffold"):
            out = _iter_search_domains(None, hint="not-a-real-domain")
        assert set(out) == set(VALID_DOMAINS)
        assert any("invalid_domain_hint_ignored" in r.message for r in caplog.records)

    def test_no_hint_keeps_legacy_full_fan_out(self):
        """When hint is None and domain is None, behavior matches pre-§17.188."""
        from app.modules.rag_pipeline import _iter_search_domains
        from app.config import VALID_DOMAINS
        assert set(_iter_search_domains(None)) == set(VALID_DOMAINS)
        assert set(_iter_search_domains(None, hint=None)) == set(VALID_DOMAINS)


# ===========================================================================
# §17.188 — _lookup_superseded result-cap
# ===========================================================================

@pytest.mark.smoke
class TestSupersedesLookupCap:
    """``_lookup_superseded`` previously used ``max(1, len(entry_ids) * 4)``
    with no upper bound. §17.188 caps it at ``settings.max_supersedes_
    lookup_results`` (default 128) and logs when the cap fires."""

    def _make_collection(self):
        collection = MagicMock()
        collection.query = MagicMock(return_value=[])
        return collection

    def test_small_lookup_uses_proposed_limit_uncapped(self, monkeypatch):
        """5 entry_ids → proposed limit = 20, well under default cap of 128 —
        cap doesn't fire, log isn't emitted."""
        from app.modules.rag_pipeline import _lookup_superseded
        collection = self._make_collection()
        _run(_lookup_superseded(collection, ["e1", "e2", "e3", "e4", "e5"]))
        # The collection.query call's `limit=` kwarg is the proposed value.
        kwargs = collection.query.call_args.kwargs
        assert kwargs["limit"] == 20

    def test_large_lookup_caps_to_settings_value(self, monkeypatch, caplog):
        """50 entry_ids → proposed limit = 200, capped to default 128."""
        import logging
        from app.config import settings
        from app.modules.rag_pipeline import _lookup_superseded

        # Lock the setting in test scope so a future default-change doesn't
        # silently re-tune this assertion.
        monkeypatch.setattr(settings, "max_supersedes_lookup_results", 128)

        collection = self._make_collection()
        entry_ids = [f"e{i}" for i in range(50)]
        with caplog.at_level(logging.WARNING, logger="scaffold"):
            _run(_lookup_superseded(collection, entry_ids))

        kwargs = collection.query.call_args.kwargs
        assert kwargs["limit"] == 128
        assert any(
            "supersedes_lookup_cap_fired" in r.message
            and "proposed_limit=200" in r.message
            and "effective_limit=128" in r.message
            for r in caplog.records
        ), "expected structured cap-fired log line"

    def test_cap_setting_respected_when_overridden(self, monkeypatch):
        """An operator that lowers the cap to 10 sees a fire at 5 entry_ids
        (proposed=20 > cap=10)."""
        from app.config import settings
        from app.modules.rag_pipeline import _lookup_superseded
        monkeypatch.setattr(settings, "max_supersedes_lookup_results", 10)
        collection = self._make_collection()
        _run(_lookup_superseded(collection, ["e1", "e2", "e3", "e4", "e5"]))
        assert collection.query.call_args.kwargs["limit"] == 10

    def test_empty_entry_ids_skips_lookup(self):
        from app.modules.rag_pipeline import _lookup_superseded
        collection = self._make_collection()
        result = _run(_lookup_superseded(collection, []))
        assert result == set()
        collection.query.assert_not_called()

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
# §17.260 — partial reranker output must trigger same fallback as empty
# ===========================================================================

class TestRagCacheHitNoMutationLeak:
    """§17.264 — cache-hit path must not mutate the cached payload, so two
    concurrent callers can't leak metadata.cache_hit=True (or any other
    mutation) into each other's responses. Pre-fix, ``cached.setdefault
    ("metadata", {}); meta["cache_hit"] = True`` mutated the dict the
    cache returned. The current Redis-backed get() yields a fresh
    json.loads dict per call so today's behavior is safe — this test
    locks in the no-shared-state invariant so a future in-process LRU
    layer in front of Redis can't reintroduce the bug. Closes
    17.258 yellow #3."""

    def test_cache_hit_does_not_mutate_source_dict(self):
        """If the cache returns the SAME Python object twice (simulating
        a hypothetical shared-ref bug from a future in-process LRU),
        query_rag must not leak cache_hit=True back into the source."""
        # Source dict that the mock cache will hand out twice — represents
        # the bytes that round-trip through Redis today, OR (post-future-
        # LRU) the in-memory cached object.
        source = {
            "status": "ok",
            "results": [],
            "metadata": {"latency_ms": 42.0},
        }
        original_metadata_id = id(source["metadata"])

        # Mock the cache to return the SAME dict twice. AsyncMock with
        # return_value returns the same object on every call — exactly
        # the shared-ref scenario the fix protects against.
        mock_cache = MagicMock()
        mock_cache.get = AsyncMock(return_value=source)

        with patch(
            "app.modules.rag_pipeline.get_rag_result_cache",
            return_value=mock_cache,
        ):
            from app.modules.rag_pipeline import query_rag
            r1 = _run(query_rag("q1", domain="eng", confidence_threshold=0.0))
            r2 = _run(query_rag("q2", domain="eng", confidence_threshold=0.0))

        # Both callers see cache_hit=True in their own response
        assert r1["metadata"]["cache_hit"] is True
        assert r2["metadata"]["cache_hit"] is True

        # But the source dict (which the cache handed out) must be UNTOUCHED:
        # no cache_hit key, and the metadata sub-dict is the original object.
        assert "cache_hit" not in source["metadata"], (
            f"cache_hit leaked back into source.metadata: {source['metadata']}"
        )
        assert id(source["metadata"]) == original_metadata_id, (
            "source metadata sub-dict was replaced (should not have been)"
        )

        # And the two responses must NOT share their outer or metadata dicts —
        # otherwise a mutation by one caller would leak into the other.
        assert r1 is not r2, "two cache hits returned the same outer dict"
        assert r1["metadata"] is not r2["metadata"], (
            "two cache hits returned the same metadata sub-dict"
        )

    def test_cache_hit_returns_cache_hit_metadata(self):
        """Sanity: the cache-hit path still surfaces cache_hit=True to the
        caller (the behavior change of the fix is internal only)."""
        source = {"status": "ok", "results": [], "metadata": {}}
        mock_cache = MagicMock()
        mock_cache.get = AsyncMock(return_value=source)

        with patch(
            "app.modules.rag_pipeline.get_rag_result_cache",
            return_value=mock_cache,
        ):
            from app.modules.rag_pipeline import query_rag
            r = _run(query_rag("q", domain="eng", confidence_threshold=0.0))

        assert r["status"] == "ok"
        assert r["metadata"]["cache_hit"] is True


class TestRerankPartialItems:
    """§17.260 — _rerank must warn + fall back to RRF when reranker returns
    fewer items than docs submitted. Pre-fix, the missing slots silently
    fell back to rrf_score via score_map.get(i, r.rrf_score), then the final
    list was sorted by final_score — mixing reranker + RRF scales produced
    undefined sort order."""

    def test_partial_items_triggers_fallback(self):
        from app.modules.rag_pipeline import _rerank, RagResult
        from app.rerankers import RerankedItem

        # Reranker sent 3 docs, returns scores for only indices [0, 2].
        # Pre-§17.260: index 1 would fall back to rrf_score=0.3 while
        # index 0 gets reranker score 0.95 and index 2 gets 0.10 — mixed
        # scales, sort order depends on which scale wins by accident.
        fake_rr = MagicMock()
        fake_rr.items = [
            RerankedItem(index=0, score=0.95, text="a"),
            RerankedItem(index=2, score=0.10, text="c"),
        ]
        fake_rr.backend = "flaky"
        fake_rr.latency_ms = 12.0

        results = [
            RagResult(content="a", entry_id="e1", rrf_score=0.5),
            RagResult(content="b", entry_id="e2", rrf_score=0.3),
            RagResult(content="c", entry_id="e3", rrf_score=0.2),
        ]

        with patch(
            "app.modules.rag_pipeline.cross_encoder_rerank",
            return_value=fake_rr,
        ):
            ranked, meta = _run(_rerank("q", results, top_k=10))

        assert meta["skipped_rerank"] is True
        assert "reranker_returned_partial_2_of_3" in meta["warnings"]
        assert meta["backend"] == "flaky"
        # Fallback: every final_score == its rrf_score (single scale).
        assert ranked[0].final_score == 0.5  # 'a'
        assert ranked[1].final_score == 0.3  # 'b'
        assert ranked[2].final_score == 0.2  # 'c'
        # Crucially: index 1 ('b') is preserved at rank 2 — pre-fix it
        # would have been outranked by index 2 ('c') because c got the
        # reranker score 0.10 while b kept rrf_score 0.3 (mixed-scale sort
        # at the boundary where reranker scores > rrf scores). Here we
        # assert the post-fix single-scale order.

    def test_full_items_unaffected(self):
        """§17.260 — when reranker returns N items for N docs, the partial
        fallback must NOT fire. Ensures the new guard didn't widen the
        existing contract."""
        from app.modules.rag_pipeline import _rerank, RagResult
        from app.rerankers import RerankedItem

        fake_rr = MagicMock()
        fake_rr.items = [
            RerankedItem(index=0, score=0.9, text="a"),
            RerankedItem(index=1, score=0.4, text="b"),
        ]
        fake_rr.backend = "healthy"
        fake_rr.latency_ms = 5.0

        results = [
            RagResult(content="a", entry_id="e1", rrf_score=0.5),
            RagResult(content="b", entry_id="e2", rrf_score=0.3),
        ]

        with patch(
            "app.modules.rag_pipeline.cross_encoder_rerank",
            return_value=fake_rr,
        ):
            ranked, meta = _run(_rerank("q", results, top_k=10))

        assert meta["skipped_rerank"] is False
        assert meta["warnings"] == []
        # Reranker scores applied; sort by score
        assert ranked[0].final_score == 0.9
        assert ranked[1].final_score == 0.4


# ===========================================================================
# §17.234 — per-request max_candidates override
# ===========================================================================

class TestRerankMaxCandidatesOverride:
    """_rerank honors per-call max_candidates; falls back to settings when None."""

    def _make_results(self, n):
        from app.modules.rag_pipeline import RagResult
        return [
            RagResult(content=f"doc-{i}", entry_id=f"e{i}", rrf_score=1.0 - i*0.01)
            for i in range(n)
        ]

    def test_override_caps_pairs_fed_to_reranker(self):
        """max_candidates=3 → reranker receives exactly 3 docs, not the global cap."""
        from app.modules.rag_pipeline import _rerank

        fake_rr = MagicMock()
        fake_rr.items = []  # reranker no-op; we only care what we pass in
        fake_rr.backend = "mock"
        fake_rr.latency_ms = 1.0

        results = self._make_results(15)

        captured_docs = []
        def capture(query, docs, top_k, max_pairs=None):
            captured_docs.extend(docs)
            return fake_rr

        with patch("app.modules.rag_pipeline.cross_encoder_rerank", side_effect=capture):
            _run(_rerank("q", results, top_k=10, max_candidates=3))

        assert len(captured_docs) == 3, f"expected 3 docs to reranker, got {len(captured_docs)}"

    def test_none_falls_back_to_settings(self):
        """max_candidates=None uses settings.rerank_max_candidates."""
        from app.modules.rag_pipeline import _rerank
        from app.config import settings

        fake_rr = MagicMock()
        fake_rr.items = []
        fake_rr.backend = "mock"
        fake_rr.latency_ms = 1.0

        # 50 input results so the cap matters regardless of settings value
        results = self._make_results(50)
        captured_docs = []
        def capture(query, docs, top_k, max_pairs=None):
            captured_docs.extend(docs)
            return fake_rr

        with patch("app.modules.rag_pipeline.cross_encoder_rerank", side_effect=capture):
            _run(_rerank("q", results, top_k=10, max_candidates=None))

        assert len(captured_docs) == int(settings.rerank_max_candidates), (
            f"expected {settings.rerank_max_candidates} (settings default), got {len(captured_docs)}"
        )

    def test_override_zero_inputs_returns_empty(self):
        """Empty results short-circuit before max_candidates is even consulted."""
        from app.modules.rag_pipeline import _rerank
        ranked, meta = _run(_rerank("q", [], top_k=10, max_candidates=5))
        assert ranked == []
        assert meta["backend"] is None

    def test_override_larger_than_results_is_safe(self):
        """max_candidates > len(results) caps at len(results) without error."""
        from app.modules.rag_pipeline import _rerank

        fake_rr = MagicMock()
        fake_rr.items = []
        fake_rr.backend = "mock"
        fake_rr.latency_ms = 1.0

        results = self._make_results(3)
        captured_docs = []
        def capture(query, docs, top_k, max_pairs=None):
            captured_docs.extend(docs)
            return fake_rr

        with patch("app.modules.rag_pipeline.cross_encoder_rerank", side_effect=capture):
            _run(_rerank("q", results, top_k=10, max_candidates=999))

        assert len(captured_docs) == 3  # Python list[:999] just stops at len(results)

    def test_max_candidates_over_20_not_silently_disabled(self):
        """§17.608 regression — max_candidates > the reranker's old _MAX_PAIRS=20
        must pass the full shortlist as max_pairs so the reranker scores all of
        it. Previously the reranker capped at 20 and the len(items)<len(docs)
        guard misread that as a partial failure and disabled reranking entirely.
        """
        from app.modules.rag_pipeline import _rerank
        from app.rerankers import RerankedItem, RerankResult

        results = self._make_results(30)
        captured = {}

        def capture(query, docs, top_k, max_pairs=None):
            captured["max_pairs"] = max_pairs
            captured["n_docs"] = len(docs)
            # Reranker honors max_pairs → returns one item per doc (full result).
            items = [RerankedItem(index=i, score=1.0 - i * 0.01, text=d)
                     for i, d in enumerate(docs)]
            return RerankResult(items=items, backend="mock", latency_ms=1.0)

        with patch("app.modules.rag_pipeline.cross_encoder_rerank", side_effect=capture):
            ranked, meta = _run(_rerank("q", results, top_k=10, max_candidates=30))

        # The whole shortlist is handed to the reranker as max_pairs...
        assert captured["max_pairs"] == captured["n_docs"] == 30
        # ...and reranking is NOT skipped (the guard no longer misfires).
        assert meta["skipped_rerank"] is False
        assert meta["backend"] == "mock"


# ===========================================================================
# §17.252 — per-request doc_truncate override
# ===========================================================================

class TestRerankDocTruncateOverride:
    """_rerank honors per-call doc_truncate; falls back to settings when None."""

    def _make_long_results(self, n, content_len):
        """Build n results whose content is exactly content_len chars."""
        from app.modules.rag_pipeline import RagResult
        return [
            RagResult(
                content="x" * content_len,
                entry_id=f"e{i}",
                rrf_score=1.0 - i * 0.01,
            )
            for i in range(n)
        ]

    def test_override_truncates_doc_chars(self):
        """doc_truncate=200 → each doc passed to the reranker is at most 200 chars."""
        from app.modules.rag_pipeline import _rerank

        fake_rr = MagicMock()
        fake_rr.items = []
        fake_rr.backend = "mock"
        fake_rr.latency_ms = 1.0

        # 5 docs × 1000 chars each — well above the override of 200.
        results = self._make_long_results(5, 1000)
        captured_docs = []

        def capture(query, docs, top_k, max_pairs=None):
            captured_docs.extend(docs)
            return fake_rr

        with patch("app.modules.rag_pipeline.cross_encoder_rerank", side_effect=capture):
            _run(_rerank("q", results, top_k=10, doc_truncate=200))

        assert all(len(d) == 200 for d in captured_docs), (
            f"expected all {len(captured_docs)} docs at exactly 200 chars; "
            f"saw lengths {[len(d) for d in captured_docs]}"
        )

    def test_none_falls_back_to_settings(self):
        """doc_truncate=None uses settings.rerank_doc_truncate."""
        from app.modules.rag_pipeline import _rerank
        from app.config import settings

        fake_rr = MagicMock()
        fake_rr.items = []
        fake_rr.backend = "mock"
        fake_rr.latency_ms = 1.0

        # Make content longer than any plausible setting so truncation
        # always bites.
        results = self._make_long_results(3, settings.rerank_doc_truncate + 500)
        captured_docs = []

        def capture(query, docs, top_k, max_pairs=None):
            captured_docs.extend(docs)
            return fake_rr

        with patch("app.modules.rag_pipeline.cross_encoder_rerank", side_effect=capture):
            _run(_rerank("q", results, top_k=10, doc_truncate=None))

        expected_len = int(settings.rerank_doc_truncate)
        assert all(len(d) == expected_len for d in captured_docs), (
            f"expected all docs truncated to {expected_len} (settings default); "
            f"saw lengths {[len(d) for d in captured_docs]}"
        )

    def test_override_with_short_content_no_padding(self):
        """doc_truncate=500 on 50-char docs returns 50-char strings (no padding)."""
        from app.modules.rag_pipeline import _rerank

        fake_rr = MagicMock()
        fake_rr.items = []
        fake_rr.backend = "mock"
        fake_rr.latency_ms = 1.0

        results = self._make_long_results(3, 50)
        captured_docs = []

        def capture(query, docs, top_k, max_pairs=None):
            captured_docs.extend(docs)
            return fake_rr

        with patch("app.modules.rag_pipeline.cross_encoder_rerank", side_effect=capture):
            _run(_rerank("q", results, top_k=10, doc_truncate=500))

        assert all(len(d) == 50 for d in captured_docs), (
            "Python slice [:500] on 50-char string returns 50-char string"
        )

    def test_independent_of_max_candidates(self):
        """doc_truncate and max_candidates compose; per-call values both apply."""
        from app.modules.rag_pipeline import _rerank

        fake_rr = MagicMock()
        fake_rr.items = []
        fake_rr.backend = "mock"
        fake_rr.latency_ms = 1.0

        results = self._make_long_results(20, 1000)
        captured_docs = []

        def capture(query, docs, top_k, max_pairs=None):
            captured_docs.extend(docs)
            return fake_rr

        with patch("app.modules.rag_pipeline.cross_encoder_rerank", side_effect=capture):
            _run(_rerank("q", results, top_k=10,
                         max_candidates=4, doc_truncate=150))

        assert len(captured_docs) == 4, (
            f"max_candidates=4 → 4 docs; got {len(captured_docs)}"
        )
        assert all(len(d) == 150 for d in captured_docs), (
            f"doc_truncate=150 → each doc 150 chars; got {[len(d) for d in captured_docs]}"
        )


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

        async def low_rerank(query, results, top_k, *, max_candidates=None, doc_truncate=None):
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

        with patch.object(rp, "_get_client", MagicMock(return_value=fake_col)), \
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


# ===========================================================================
# §17.253 — reranker-knob resolution surfaced in /rag metadata
# ===========================================================================

class TestRerankMetadataResolution:
    """metadata.rerank_{max_candidates,doc_truncate} reflect the effective
    values used: settings.* when no per-request override, explicit value
    when overridden."""

    def test_metadata_shows_settings_defaults_when_no_override(self):
        from app.modules.rag_pipeline import query_rag
        from app.config import settings

        with _PatchStack(_patch_rag_deps()):
            result = _run(query_rag("q", domain="eng", confidence_threshold=0.0))
        md = result["metadata"]
        assert md["rerank_max_candidates"] == int(settings.rerank_max_candidates)
        assert md["rerank_doc_truncate"]   == int(settings.rerank_doc_truncate)

    def test_metadata_shows_explicit_override_values(self):
        from app.modules.rag_pipeline import query_rag

        with _PatchStack(_patch_rag_deps()):
            result = _run(query_rag(
                "q", domain="eng", confidence_threshold=0.0,
                max_candidates=7, doc_truncate=750,
            ))
        md = result["metadata"]
        assert md["rerank_max_candidates"] == 7
        assert md["rerank_doc_truncate"]   == 750

    def test_metadata_shows_resolved_int_when_one_axis_overridden(self):
        """Override one axis, leave the other default — metadata shows the
        explicit override + the settings fallback as resolved ints."""
        from app.modules.rag_pipeline import query_rag
        from app.config import settings

        with _PatchStack(_patch_rag_deps()):
            result = _run(query_rag(
                "q", domain="eng", confidence_threshold=0.0,
                max_candidates=3,  # explicit
                # doc_truncate omitted → settings default
            ))
        md = result["metadata"]
        assert md["rerank_max_candidates"] == 3
        assert md["rerank_doc_truncate"]   == int(settings.rerank_doc_truncate)


# ===========================================================================
# §17.255 — reranker_decision log line carries the effective knob values
# ===========================================================================

class TestRerankDecisionLogContent:
    """The §17.254 log fields lock the operator-grep contract.

    `docker logs scaffold-orchestrator | grep '"rerank_max_candidates": 5'`
    is the operator's way of finding every call that ran at max=5.
    Without these tests, a future refactor that drops the fields
    from the `extra` dict would break that recipe silently.
    """

    def _make_results(self, n):
        from app.modules.rag_pipeline import RagResult
        return [
            RagResult(content="x" * 100, entry_id=f"e{i}", rrf_score=1.0 - i*0.01)
            for i in range(n)
        ]

    def _mock_rr(self, n_items):
        """Build a fake RerankResult with N scored items."""
        mock = MagicMock()
        mock.items = [
            MagicMock(index=i, score=0.5 + i*0.01, text=f"doc{i}")
            for i in range(n_items)
        ]
        mock.backend = "MockCE"
        mock.latency_ms = 5.0
        return mock

    def test_log_carries_settings_defaults_when_no_override(self, caplog):
        import logging
        from app.modules.rag_pipeline import _rerank
        from app.config import settings

        with caplog.at_level(logging.INFO, logger="scaffold.rag"):
            with patch("app.modules.rag_pipeline.cross_encoder_rerank",
                       return_value=self._mock_rr(3)):
                _run(_rerank("q", self._make_results(3), top_k=10))

        recs = [r for r in caplog.records if r.message == "reranker_decision"]
        assert len(recs) == 1
        r = recs[0]
        assert r.rerank_max_candidates == int(settings.rerank_max_candidates)
        assert r.rerank_doc_truncate   == int(settings.rerank_doc_truncate)

    def test_log_carries_explicit_override_values(self, caplog):
        import logging
        from app.modules.rag_pipeline import _rerank

        with caplog.at_level(logging.INFO, logger="scaffold.rag"):
            with patch("app.modules.rag_pipeline.cross_encoder_rerank",
                       return_value=self._mock_rr(3)):
                _run(_rerank("q", self._make_results(3), top_k=10,
                             max_candidates=7, doc_truncate=750))

        recs = [r for r in caplog.records if r.message == "reranker_decision"]
        assert len(recs) == 1
        r = recs[0]
        assert r.rerank_max_candidates == 7
        assert r.rerank_doc_truncate   == 750

    def test_log_carries_resolved_int_when_one_axis_overridden(self, caplog):
        import logging
        from app.modules.rag_pipeline import _rerank
        from app.config import settings

        with caplog.at_level(logging.INFO, logger="scaffold.rag"):
            with patch("app.modules.rag_pipeline.cross_encoder_rerank",
                       return_value=self._mock_rr(3)):
                _run(_rerank("q", self._make_results(3), top_k=10,
                             max_candidates=4))
                # doc_truncate omitted → settings default

        recs = [r for r in caplog.records if r.message == "reranker_decision"]
        assert len(recs) == 1
        r = recs[0]
        assert r.rerank_max_candidates == 4
        assert r.rerank_doc_truncate   == int(settings.rerank_doc_truncate)

    def test_log_field_types_are_int_never_none(self, caplog):
        """Defensive: even with no override, fields must be int (not None
        or string). Catches a future refactor that accidentally swaps
        the resolved-int for the raw input."""
        import logging
        from app.modules.rag_pipeline import _rerank

        with caplog.at_level(logging.INFO, logger="scaffold.rag"):
            with patch("app.modules.rag_pipeline.cross_encoder_rerank",
                       return_value=self._mock_rr(3)):
                _run(_rerank("q", self._make_results(3), top_k=10))

        recs = [r for r in caplog.records if r.message == "reranker_decision"]
        assert len(recs) == 1
        r = recs[0]
        assert isinstance(r.rerank_max_candidates, int)
        assert isinstance(r.rerank_doc_truncate, int)
        assert r.rerank_max_candidates is not None
        assert r.rerank_doc_truncate is not None
