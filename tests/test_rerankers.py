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


# ---------------------------------------------------------------------------
# §17.187 — score normalization registry
# ---------------------------------------------------------------------------
#
# The downstream confidence threshold (default 0.8) is sensitive to the
# reranker's raw output range. The §17.187 registry maps known model
# patterns to (range_label, normalizer) so the threshold survives a
# MODEL_RERANKER swap that emits raw logits rather than already-sigmoid'd
# probabilities. Tests cover: identity for the production reranker, sigmoid
# for the two known logit-emitting CrossEncoder families, conservative
# fallback for unknown models, the normalizer math itself, and end-to-end
# threading through ``rerank_cross_encoder``.


@pytest.mark.smoke
def test_get_score_range_info_recognizes_qwen3_reranker_identity():
    label, fn = rerankers.get_score_range_info(
        "tomaarsen/Qwen3-Reranker-0.6B-seq-cls",
    )
    assert "0, 1" in label
    assert "sigmoid" in label.lower()
    # Identity short-circuits to ``list(scores)`` — no transform.
    assert fn([0.3, 0.7]) == [0.3, 0.7]


@pytest.mark.smoke
@pytest.mark.parametrize("name", [
    "cross-encoder/ms-marco-MiniLM-L-6-v2",
    "cross-encoder/ms-marco-MiniLM-L-12-v2",
    "MS-MARCO/some-future-variant",
])
def test_get_score_range_info_recognizes_ms_marco_as_logits(name):
    label, fn = rerankers.get_score_range_info(name)
    assert "logit" in label.lower()
    out = fn([0.0])
    assert out[0] == pytest.approx(0.5, abs=1e-9)


@pytest.mark.smoke
@pytest.mark.parametrize("name", [
    "BAAI/bge-reranker-base",
    "BAAI/bge-reranker-large",
    "bge-reranker-v2-m3",
])
def test_get_score_range_info_recognizes_bge_reranker_as_logits(name):
    label, fn = rerankers.get_score_range_info(name)
    assert "logit" in label.lower()
    # Sigmoid bounds: large negative → ~0, large positive → ~1.
    assert fn([-100.0])[0] == pytest.approx(0.0, abs=1e-9)
    assert fn([100.0])[0] == pytest.approx(1.0, abs=1e-9)


@pytest.mark.smoke
def test_get_score_range_info_unknown_model_assumes_identity_with_warning_label():
    """An unregistered model gets identity (conservative) + a label that
    flags the gap so an operator reading /health sees the uncertainty."""
    label, fn = rerankers.get_score_range_info("vendor/some-future-reranker")
    assert "unknown" in label.lower()
    # Identity behavior — same list back.
    assert fn([0.42, 0.99]) == [0.42, 0.99]


@pytest.mark.smoke
def test_get_score_range_info_none_returns_no_model_label():
    """When settings.model_reranker is unset, label is distinct from the
    unknown-model case so an operator can tell config-missing apart from
    config-set-to-something-we-don't-recognize."""
    label, fn = rerankers.get_score_range_info(None)
    assert "no model" in label.lower()
    assert fn([0.5]) == [0.5]


@pytest.mark.smoke
def test_get_score_range_info_match_is_case_insensitive():
    """Match should tolerate org-prefix capitalization variants ('Qwen3'
    vs 'qwen3' vs 'QWEN3') so the registry is robust against the HF naming
    drift between published model tags."""
    for name in ("Qwen3-Reranker-0.6B", "QWEN3-RERANKER", "vendor/qwen3-reranker"):
        label, _ = rerankers.get_score_range_info(name)
        assert "0, 1" in label, f"failed for name={name!r}"


@pytest.mark.smoke
def test_normalize_sigmoid_math():
    """Sigmoid: σ(0)=0.5, σ(+x)→1, σ(-x)→0, monotone increasing."""
    out = rerankers._normalize_sigmoid([-10.0, -1.0, 0.0, 1.0, 10.0])
    assert out[2] == pytest.approx(0.5, abs=1e-9)
    assert out == sorted(out)  # monotone increasing
    assert all(0.0 <= s <= 1.0 for s in out)


@pytest.mark.smoke
def test_normalize_sigmoid_returns_independent_list():
    """Normalizer must not mutate the input — callers compare raw vs normalized."""
    inp = [0.0, 1.0]
    out = rerankers._normalize_sigmoid(inp)
    assert out is not inp
    assert inp == [0.0, 1.0]  # input unchanged


@pytest.mark.smoke
def test_normalize_identity_returns_copy():
    inp = [0.3, 0.7]
    out = rerankers._normalize_identity(inp)
    assert out == inp
    assert out is not inp  # copy, not alias


# ---------------------------------------------------------------------------
# §17.187 — rerank_cross_encoder applies the normalizer end-to-end
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_rerank_cross_encoder_applies_sigmoid_for_logit_model(monkeypatch):
    """When MODEL_RERANKER is a ms-marco model, raw model.predict outputs
    (logits) get squashed to [0,1] before being returned as RerankedItem.score."""
    from app.config import settings
    monkeypatch.setattr(
        settings, "model_reranker", "cross-encoder/ms-marco-MiniLM-L-6-v2",
    )
    model = MagicMock()
    # Raw logits — without sigmoid, 5.0 would blow past the 0.8 threshold trivially.
    model.predict.return_value = [-2.0, 0.0, 5.0]
    with patch.object(rerankers, "_get_cross_encoder", return_value=model):
        result = rerankers.rerank_cross_encoder("q", ["a", "b", "c"], top_k=3)
    assert result is not None
    # All scores in [0, 1] post-normalization.
    for it in result.items:
        assert 0.0 <= it.score <= 1.0
    # Top score is σ(5.0) ≈ 0.9933.
    assert result.items[0].score == pytest.approx(0.9933, abs=1e-3)
    # σ(0.0) = 0.5 — middle.
    assert any(it.score == pytest.approx(0.5, abs=1e-9) for it in result.items)


@pytest.mark.smoke
def test_rerank_cross_encoder_leaves_qwen3_scores_unchanged(monkeypatch):
    """The default production reranker (Qwen3) emits ~[0,1] already;
    identity normalizer must not perturb existing behavior."""
    from app.config import settings
    monkeypatch.setattr(
        settings, "model_reranker", "tomaarsen/Qwen3-Reranker-0.6B-seq-cls",
    )
    model = MagicMock()
    model.predict.return_value = [0.2, 0.95, 0.55]
    with patch.object(rerankers, "_get_cross_encoder", return_value=model):
        result = rerankers.rerank_cross_encoder("q", ["a", "b", "c"], top_k=3)
    assert result is not None
    # Scores returned unchanged (identity normalizer); 0.95 still the top.
    assert result.items[0].score == pytest.approx(0.95, abs=1e-9)
    assert result.items[1].score == pytest.approx(0.55, abs=1e-9)
    assert result.items[2].score == pytest.approx(0.2, abs=1e-9)
