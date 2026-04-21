"""Tests for app/rerankers.py (#9.24)."""
from unittest.mock import MagicMock, patch

import pytest

from app import rerankers


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Each test starts with a clean reranker state."""
    rerankers.reset_reranker()
    yield
    rerankers.reset_reranker()


# ---------------------------------------------------------------------------
# RRF fallback (no model needed)
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_rerank_rrf_preserves_order():
    result = rerankers.rerank_rrf(["a", "b", "c"], top_k=3)
    assert result.backend == "RRF"
    assert [it.index for it in result.items] == [0, 1, 2]
    assert [it.text for it in result.items] == ["a", "b", "c"]


@pytest.mark.smoke
def test_rerank_rrf_scores_decrease_monotonically():
    result = rerankers.rerank_rrf(["a", "b", "c", "d"], top_k=4)
    scores = [it.score for it in result.items]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.smoke
def test_rerank_rrf_top_k_truncates():
    result = rerankers.rerank_rrf(["a", "b", "c", "d", "e"], top_k=2)
    assert len(result.items) == 2


# ---------------------------------------------------------------------------
# CrossEncoder path (mocked)
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_rerank_cross_encoder_sorts_by_score_desc():
    model = MagicMock()
    model.predict.return_value = [0.2, 0.9, 0.5]
    with patch.object(rerankers, "_get_cross_encoder", return_value=model):
        result = rerankers.rerank_cross_encoder("q", ["a", "b", "c"], top_k=3)
    assert result is not None
    assert result.backend == "CrossEncoder"
    assert [it.index for it in result.items] == [1, 2, 0]


@pytest.mark.smoke
def test_rerank_cross_encoder_respects_top_k():
    model = MagicMock()
    model.predict.return_value = [0.1, 0.2, 0.3, 0.4, 0.5]
    with patch.object(rerankers, "_get_cross_encoder", return_value=model):
        result = rerankers.rerank_cross_encoder("q", ["a", "b", "c", "d", "e"], top_k=2)
    assert len(result.items) == 2


@pytest.mark.smoke
def test_rerank_cross_encoder_caps_at_max_pairs():
    model = MagicMock()
    # Return len(docs) scores so the call doesn't crash
    model.predict.side_effect = lambda pairs: [0.5] * len(pairs)
    docs = [f"doc{i}" for i in range(50)]
    with patch.object(rerankers, "_get_cross_encoder", return_value=model):
        rerankers.rerank_cross_encoder("q", docs, top_k=5)
    # Only _MAX_PAIRS docs should have been passed to predict
    args, _ = model.predict.call_args
    assert len(args[0]) == rerankers._MAX_PAIRS


@pytest.mark.smoke
def test_rerank_cross_encoder_returns_none_on_load_failure():
    with patch.object(rerankers, "_get_cross_encoder", return_value=None):
        assert rerankers.rerank_cross_encoder("q", ["a"], top_k=1) is None


@pytest.mark.smoke
def test_rerank_cross_encoder_returns_none_on_inference_error():
    model = MagicMock()
    model.predict.side_effect = RuntimeError("oom")
    with patch.object(rerankers, "_get_cross_encoder", return_value=model):
        assert rerankers.rerank_cross_encoder("q", ["a"], top_k=1) is None


# ---------------------------------------------------------------------------
# Public rerank() — CrossEncoder → RRF fallback
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_rerank_falls_back_to_rrf_when_ce_unavailable():
    with patch.object(rerankers, "_get_cross_encoder", return_value=None):
        result = rerankers.rerank("q", ["a", "b"], top_k=2)
    assert result.backend == "RRF"


@pytest.mark.smoke
def test_rerank_prefers_cross_encoder_when_available():
    model = MagicMock()
    model.predict.return_value = [0.5, 0.8]
    with patch.object(rerankers, "_get_cross_encoder", return_value=model):
        result = rerankers.rerank("q", ["a", "b"], top_k=2)
    assert result.backend == "CrossEncoder"


# ---------------------------------------------------------------------------
# Singleton / reset behaviour
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_reset_reranker_clears_singleton_and_failure_flag():
    rerankers._cross_encoder = MagicMock()
    rerankers._load_failed = True
    rerankers.reset_reranker()
    assert rerankers._cross_encoder is None
    assert rerankers._load_failed is False
