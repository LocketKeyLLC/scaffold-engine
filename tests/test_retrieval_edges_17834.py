"""§17.834 (plan 8.4 / audit M9) — retrieval edge honesty.

  - rerank cap: top_k > rerank_max_candidates warns + annotates instead of
    silently returning ≤ cap rows
  - supersedes sweep: swept rows are BACKFILLED from the ranked pool
    (pre-fix the sweep ran post-slice and just shrank the response)
  - provenance batch-fetch failure appends a response warning (it also
    silently zeroes every §17.120 quality bump)
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from tests.test_rag_pipeline import (
    _PatchStack,
    _make_vector_results,
    _patch_rag_deps,
    _run,
)

pytestmark = pytest.mark.smoke


# ---------------------------------------------------------------------------
# _rerank cap honesty (real function, CrossEncoder boundary mocked)
# ---------------------------------------------------------------------------

class _FakeRerankResult:
    def __init__(self, n):
        class _Item:
            def __init__(self, index, score):
                self.index = index
                self.score = score
        self.items = [_Item(i, 1.0 - i * 0.01) for i in range(n)]
        self.backend = "CrossEncoder"
        self.latency_ms = 5.0


def _results(n):
    from app.modules.rag_pipeline import RagResult
    return [
        RagResult(content=f"c{i}", entry_id=f"e{i}", rrf_score=0.03 - i * 0.001)
        for i in range(n)
    ]


class TestRerankCapHonesty:
    def _rerank(self, results, top_k, max_candidates):
        from app.modules import rag_pipeline as rp

        def fake_ce(query, docs, *a, **k):
            return _FakeRerankResult(len(docs))

        with patch.object(rp, "cross_encoder_rerank", side_effect=fake_ce):
            return _run(rp._rerank("q", results, top_k,
                                   max_candidates=max_candidates))

    def test_top_k_beyond_cap_warns_and_annotates(self):
        ranked, meta = self._rerank(_results(15), top_k=15, max_candidates=10)
        assert "rerank_cap_truncation" in meta["warnings"]
        assert meta["rerank_capped_at"] == 10
        assert len(ranked) == 15  # §17.834 — full list returned, caller slices

    def test_top_k_within_cap_no_warning(self):
        ranked, meta = self._rerank(_results(15), top_k=5, max_candidates=10)
        assert "rerank_cap_truncation" not in meta["warnings"]
        assert "rerank_capped_at" not in meta

    def test_few_results_no_warning(self):
        """Everything got scored — a big top_k alone isn't a truncation."""
        ranked, meta = self._rerank(_results(4), top_k=15, max_candidates=10)
        assert "rerank_cap_truncation" not in meta["warnings"]


# ---------------------------------------------------------------------------
# Supersedes sweep backfill (query_rag with the standard harness)
# ---------------------------------------------------------------------------

def _six_results():
    return _make_vector_results([
        {"entry_id": f"e{i}", "content": f"Result {i}",
         "vector_score": 0.95 - i * 0.05}
        for i in range(1, 7)
    ])


class TestSupersedesBackfill:
    def test_swept_row_is_backfilled_to_top_k(self):
        patches = _patch_rag_deps(
            vector_results=_six_results(), superseded_ids={"e2"},
        )
        with _PatchStack(patches):
            from app.modules.rag_pipeline import query_rag
            result = _run(query_rag(
                "q", domain="eng", top_k=3, confidence_threshold=0.0,
            ))
        ids = [r["entry_id"] for r in result["results"]]
        assert len(ids) == 3  # still top_k despite the sweep
        assert "e2" not in ids
        assert "e4" in ids  # next-best pool row filled the slot
        assert result["metadata"]["superseded_dropped"] == 1
        assert result["metadata"]["superseded_backfilled"] == 1

    def test_exhausted_pool_returns_short_with_accounting(self):
        patches = _patch_rag_deps(
            vector_results=_six_results(),
            superseded_ids={"e2", "e4", "e5", "e6"},  # every candidate too
        )
        with _PatchStack(patches):
            from app.modules.rag_pipeline import query_rag
            result = _run(query_rag(
                "q", domain="eng", top_k=3, confidence_threshold=0.0,
            ))
        ids = [r["entry_id"] for r in result["results"]]
        assert ids == ["e1", "e3"]  # short, honestly
        assert result["metadata"]["superseded_dropped"] == 1
        assert result["metadata"]["superseded_backfilled"] == 0

    def test_no_sweep_zero_accounting(self):
        patches = _patch_rag_deps(vector_results=_six_results())
        with _PatchStack(patches):
            from app.modules.rag_pipeline import query_rag
            result = _run(query_rag(
                "q", domain="eng", top_k=3, confidence_threshold=0.0,
            ))
        assert result["metadata"]["superseded_dropped"] == 0
        assert result["metadata"]["superseded_backfilled"] == 0


# ---------------------------------------------------------------------------
# Provenance-fetch failure warning
# ---------------------------------------------------------------------------

class TestProvenanceFailureWarning:
    def test_failed_batch_fetch_appends_warning(self):
        patches = _patch_rag_deps(vector_results=_six_results())
        with _PatchStack(patches), \
             patch("app.modules.rag_pipeline.get_provenance_batch",
                   AsyncMock(side_effect=RuntimeError("pg down"))):
            from app.modules.rag_pipeline import query_rag
            result = _run(query_rag(
                "q", domain="eng", top_k=3, confidence_threshold=0.0,
            ))
        assert "provenance_fetch_failed" in result["metadata"]["warnings"]
        assert result["result_count"] == 3  # fail-soft: results still served

    def test_ok_fetch_no_warning(self):
        patches = _patch_rag_deps(vector_results=_six_results())
        with _PatchStack(patches):
            from app.modules.rag_pipeline import query_rag
            result = _run(query_rag(
                "q", domain="eng", top_k=3, confidence_threshold=0.0,
            ))
        assert "provenance_fetch_failed" not in result["metadata"]["warnings"]
