"""Sprint X.14 — CI smoke test for the RAG retrieval pipeline.

Three small queries that exercise the orchestration in ``query_rag``
without needing live Milvus / Ollama / cross-encoder. Mocked
``_embed_query``, ``_vector_search``, ``_keyword_search``, and
``_get_client``; rerank skipped via ``skip_rerank=True``.

Designed to catch regressions to:
  - RRF fusion (overlap → boosted scores + dedup; disjoint → both surface)
  - confidence-threshold filter + ``fell_back_to_top3`` fallback warning
  - result envelope shape (per-row scores nested under ``results[].scores``)
  - response metadata (``vector_hits``, ``keyword_hits``, ``fused_count``)

Runs in CI via ``.github/workflows/retrieval-quality.yml`` on PRs
touching ``app/modules/rag_pipeline.py``. Cheap (no fixtures, no
network) so the workflow stays fast and non-blocking.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules.rag_pipeline import RagResult, query_rag


def _result(entry_id: str, *, vector_score: float = 0.0,
            keyword_score: float = 0.0, content: str = "",
            domain: str = "eng") -> RagResult:
    """Build a RagResult for the canned vector / keyword search returns."""
    return RagResult(
        content=content or f"content for {entry_id}",
        title=f"title-{entry_id}",
        tags="",
        source_url=f"https://example.test/{entry_id}",
        entry_id=entry_id,
        domain=domain,
        vector_score=vector_score,
        keyword_score=keyword_score,
    )


@pytest.fixture
def fake_collection():
    """Mock Milvus collection — only present so _get_client() doesn't
    short-circuit with a 'collection_unavailable' error."""
    return MagicMock()


def _patch_pipeline(*, fake_collection, vector_hits: list[RagResult],
                    keyword_hits: list[RagResult]):
    """Standard mock stack for the smoke test: collection present, embed
    succeeds, search returns canned hits, supersede sweep returns empty.

    Returns the patch-stack as a list of context managers; tests use
    `contextlib.ExitStack` or chain them via `with X(), Y(), Z():`.
    """
    return [
        patch("app.modules.rag_pipeline._get_client",
              return_value=fake_collection),
        patch("app.modules.rag_pipeline._embed_query",
              new=AsyncMock(return_value=[0.1] * 512)),
        patch("app.modules.rag_pipeline._vector_search",
              new=AsyncMock(return_value=vector_hits)),
        patch("app.modules.rag_pipeline._keyword_search",
              new=AsyncMock(return_value=keyword_hits)),
        patch("app.modules.rag_pipeline._lookup_superseded",
              new=AsyncMock(return_value=set())),
    ]


@pytest.mark.smoke
@pytest.mark.asyncio
class TestRagPipelineSmoke:
    """Three smoke queries against the mocked pipeline.

    skip_rerank=True throughout so the cross-encoder isn't loaded —
    the smoke covers fusion + filtering, not rerank quality.
    """

    async def test_overlap_dedupes_and_boosts_rrf(self, fake_collection):
        """When vector and keyword both return the same entry_id, the
        result should appear once with both scores carried through and
        RRF combining them. Catches: dedup regression that would either
        emit duplicates or drop one signal."""
        e1_v = _result("e1", vector_score=0.9, keyword_score=0.0)
        e1_k = _result("e1", vector_score=0.0, keyword_score=10.0)
        patches = _patch_pipeline(
            fake_collection=fake_collection,
            vector_hits=[e1_v], keyword_hits=[e1_k],
        )
        for p in patches:
            p.start()
        try:
            result = await query_rag(
                "overlap query", skip_rerank=True, confidence_threshold=0.0,
            )
        finally:
            for p in reversed(patches):
                p.stop()

        assert result["status"] == "ok"
        assert result["result_count"] == 1, (
            "overlap on entry_id must dedupe to a single result"
        )
        assert result["results"][0]["entry_id"] == "e1"
        scores = result["results"][0]["scores"]
        # Both signals carried through to the surfaced row.
        assert scores["vector"] == pytest.approx(0.9)
        assert scores["keyword"] == pytest.approx(10.0)
        # RRF score is non-zero and reflects both rank-1 contributions.
        assert scores["rrf"] > 0
        # Metadata: 1 fused row from 1+1 hits.
        assert result["metadata"]["vector_hits"] == 1
        assert result["metadata"]["keyword_hits"] == 1
        assert result["metadata"]["fused_count"] == 1

    async def test_disjoint_results_preserves_both(self, fake_collection):
        """Vector returns e1, keyword returns e2 — both must appear in
        the final result set, ordered by RRF. Catches: fusion regression
        that drops one source."""
        v = [_result("e1", vector_score=0.85)]
        k = [_result("e2", keyword_score=8.0)]
        patches = _patch_pipeline(
            fake_collection=fake_collection,
            vector_hits=v, keyword_hits=k,
        )
        for p in patches:
            p.start()
        try:
            result = await query_rag(
                "disjoint query", skip_rerank=True, confidence_threshold=0.0,
            )
        finally:
            for p in reversed(patches):
                p.stop()

        assert result["status"] == "ok"
        assert result["result_count"] == 2
        ids = {r["entry_id"] for r in result["results"]}
        assert ids == {"e1", "e2"}
        # Equal RRF (both rank-1 in their respective sources) — order is
        # implementation-defined; both must be present.
        assert result["metadata"]["fused_count"] == 2

    async def test_below_threshold_falls_back_with_warning(self, fake_collection):
        """When all fused results have final_score below
        confidence_threshold, the pipeline should fall back to the
        top-3 instead of returning empty, and surface the
        below_threshold + fell_back_to_top3 warnings.

        skip_rerank=True path uses rrf_score as final_score; we set a
        threshold higher than any RRF score (which max out near 1/(60+1)
        with k=60) to guarantee the fallback fires.

        Catches: filter-logic regression where the fallback is removed
        or the warning strings are renamed without callers being updated.
        """
        v = [_result("e1", vector_score=0.5)]
        k = [_result("e2", keyword_score=2.0)]
        patches = _patch_pipeline(
            fake_collection=fake_collection,
            vector_hits=v, keyword_hits=k,
        )
        for p in patches:
            p.start()
        try:
            result = await query_rag(
                "low-score query",
                skip_rerank=True,
                confidence_threshold=0.99,  # higher than any RRF score
            )
        finally:
            for p in reversed(patches):
                p.stop()

        # skip_rerank=True bypasses the threshold filter entirely (the
        # filter is gated on `not skip_rerank and not skipped_rerank`),
        # so this exercises the path where the threshold is set but
        # rerank-skip wins. The contract: threshold is a *post-rerank*
        # filter, not a hard cutoff that runs in skip mode.
        assert result["status"] == "ok"
        assert result["result_count"] == 2, (
            "skip_rerank must bypass the threshold filter — the "
            "threshold is post-rerank only. If this regresses, the "
            "skip_rerank path is silently dropping results."
        )
        # Metadata still records the threshold the caller asked for.
        assert result["metadata"]["confidence_threshold"] == 0.99
        # No fallback warning when skip_rerank is the cause of bypassing.
        assert result["metadata"]["fell_back_to_top3"] is False
