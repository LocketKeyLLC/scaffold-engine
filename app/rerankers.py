"""rerankers.py — CrossEncoder reranker with RRF fallback."""
from __future__ import annotations

import logging
import math
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)

# Max query-document pairs to score per rerank call (CrossEncoder input cap)
_MAX_PAIRS = 20


# ---------------------------------------------------------------------------
# §17.187 — Per-reranker score normalization registry.
#
# Different CrossEncoder models emit scores on different ranges:
#
#   * Qwen3-Reranker (production default) post-sigmoids inside the model;
#     scores arrive in roughly [0, 1] — identity is correct.
#   * cross-encoder/ms-marco-* outputs raw logits, roughly [-10, +10]; the
#     downstream confidence threshold (0.8 by default) becomes either
#     trivially-met or never-met without normalization.
#   * BAAI/bge-reranker-* (large + base) similarly emits raw logits.
#
# MODEL_RERANKER is config-only per the project invariants (cannot be
# swapped per-request) so the normalizer is selected once at the call
# site against ``settings.model_reranker``. A model not in the registry
# is conservatively assumed to be already-[0,1] (identity) and reported
# as "unknown (assumed [0,1])" on /health so an operator sees the gap.
#
# Match is substring-on-lowercased name so we tolerate the common
# variants (model org prefix, version suffix, etc.) without enumerating
# every published tag.
# ---------------------------------------------------------------------------

def _normalize_identity(scores: list[float]) -> list[float]:
    """Pass-through — score is already in the expected [0, 1] range."""
    return list(scores)


def _normalize_sigmoid(scores: list[float]) -> list[float]:
    """Apply sigmoid to map raw logits → (0, 1)."""
    return [1.0 / (1.0 + math.exp(-float(s))) for s in scores]


# Pairs are (substring-in-model-name-lowercased, range_label, normalizer).
# Order matters — first match wins.
_RERANKER_NORMALIZERS: list[tuple[str, str, Callable[[list[float]], list[float]]]] = [
    ("qwen3-reranker", "[0, 1] (already-sigmoid)", _normalize_identity),
    ("ms-marco", "raw logits → sigmoid", _normalize_sigmoid),
    ("bge-reranker", "raw logits → sigmoid", _normalize_sigmoid),
]


def get_score_range_info(
    model_name: str | None,
) -> tuple[str, Callable[[list[float]], list[float]]]:
    """Return (range_label, normalizer) for the reranker ``model_name``.

    Unknown models are assumed already-[0, 1] (identity) so the production
    threshold survives a swap that didn't get registered here — but the
    range_label flags the gap to an operator reading /health.
    """
    if not model_name:
        return ("unknown (no model configured)", _normalize_identity)
    name = model_name.lower()
    for pat, label, fn in _RERANKER_NORMALIZERS:
        if pat in name:
            return (label, fn)
    return ("unknown (assumed [0, 1])", _normalize_identity)

# ---------------------------------------------------------------------------
# Lazy-loaded CrossEncoder singleton
# ---------------------------------------------------------------------------
_cross_encoder = None
_load_failed = False
_load_lock = threading.Lock()


def reset_reranker():
    """Reset reranker state so next call retries loading."""
    global _cross_encoder, _load_failed
    with _load_lock:
        _cross_encoder = None
        _load_failed = False


def _get_cross_encoder():
    """Load model once, on first call. Returns None if unavailable.

    Uses double-checked locking so concurrent first calls don\'t trigger
    multiple ~13s CrossEncoder loads.
    """
    global _cross_encoder, _load_failed
    # Fast path (no lock) — hot path after initial load
    if _cross_encoder is not None:
        return _cross_encoder
    if _load_failed:
        return None

    with _load_lock:
        # Recheck under lock
        if _cross_encoder is not None:
            return _cross_encoder
        if _load_failed:
            return None
        from sentence_transformers import CrossEncoder
        from app.config import settings
        model_name = settings.model_reranker

        # Retry with exponential backoff (transient network/disk stalls during
        # cold HF cache load shouldn't permanently disable the reranker)
        _MAX_ATTEMPTS = 3
        _BASE_DELAY_S = 2.0

        last_err: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                logger.info(
                    "crossencoder_loading: model=%s attempt=%d/%d",
                    model_name, attempt, _MAX_ATTEMPTS,
                )
                t0 = time.monotonic()
                _cross_encoder = CrossEncoder(model_name, trust_remote_code=True)
                elapsed = time.monotonic() - t0
                logger.info("crossencoder_loaded: elapsed_s=%.1f", elapsed)
                return _cross_encoder
            except Exception as e:
                last_err = e
                if attempt < _MAX_ATTEMPTS:
                    # Full-jitter exponential backoff: decorrelates concurrent
                    # cold-loads against the HF cache when multiple processes
                    # restart in lockstep (e.g. compose recreate).
                    capped = _BASE_DELAY_S * (2 ** (attempt - 1))
                    delay = random.uniform(0.0, capped)
                    logger.warning(
                        "crossencoder_load_retry: attempt=%d/%d error=%s retry_in=%.1fs",
                        attempt, _MAX_ATTEMPTS, e, delay,
                    )
                    # Sync sleep is intentional: callers invoke this via
                    # run_in_executor, so we are off the event loop.
                    time.sleep(delay)

        _load_failed = True
        logger.error(
            "crossencoder_load_failed: attempts=%d last_error=%s",
            _MAX_ATTEMPTS, last_err,
        )
        return None




# ---------------------------------------------------------------------------
# Reranker prompt template (config-driven; defaults match Qwen3-Reranker)
# ---------------------------------------------------------------------------
def _format_query(query: str, instruction: str | None = None) -> str:
    from app.config import settings
    inst = instruction or settings.reranker_default_instruction
    return f"{settings.reranker_prompt_system}<Instruct>: {inst}\n<Query>: {query}\n"


def _format_document(document: str) -> str:
    from app.config import settings
    return f"<Document>: {document}{settings.reranker_prompt_suffix}"

# ---------------------------------------------------------------------------
# Data objects
# ---------------------------------------------------------------------------
@dataclass
class RerankedItem:
    index: int
    score: float
    text: str
    metadata: dict = field(default_factory=dict)


@dataclass
class RerankResult:
    items: list[RerankedItem]
    backend: str
    latency_ms: float = 0.0


# ---------------------------------------------------------------------------
# CrossEncoder reranker
# ---------------------------------------------------------------------------
def rerank_cross_encoder(
    query: str,
    documents: list[str],
    top_k: int = 5,
    max_pairs: int = _MAX_PAIRS,
) -> RerankResult | None:
    """Score query-document pairs via CrossEncoder. Returns None on failure.

    Raw scores are model-dependent (see ``get_score_range_info``); §17.187
    normalizes them to a stable [0, 1] range before returning so the
    downstream confidence threshold (``settings.confidence_threshold``)
    survives a reranker model swap without per-deployment retuning.
    """
    model = _get_cross_encoder()
    if model is None:
        return None

    docs = documents[:max_pairs]
    pairs = [[_format_query(query), _format_document(doc)] for doc in docs]

    try:
        from app.config import settings
        t0 = time.monotonic()
        raw_scores = model.predict(pairs)
        # §17.187 — apply the per-model normalizer so threshold semantics
        # are stable across reranker swaps.
        _, normalize = get_score_range_info(settings.model_reranker)
        scores = normalize([float(s) for s in raw_scores])
        elapsed_ms = (time.monotonic() - t0) * 1000

        items = [
            RerankedItem(index=i, score=float(s), text=docs[i])
            for i, s in enumerate(scores)
        ]
        items.sort(key=lambda x: x.score, reverse=True)
        items = items[:top_k]

        logger.info(
            "reranker_completed: docs=%d elapsed_ms=%.0f top_score=%.4f",
            len(docs), elapsed_ms, items[0].score if items else 0,
        )
        return RerankResult(
            items=items, backend="CrossEncoder", latency_ms=elapsed_ms,
        )
    except Exception as e:
        logger.warning("crossencoder_inference_failed: error=%s", e)
        return None


# ---------------------------------------------------------------------------
# RRF fallback (no model needed)
# ---------------------------------------------------------------------------
def rerank_rrf(
    documents: list[str],
    top_k: int = 5,
    k: int = 60,
) -> RerankResult:
    """Reciprocal Rank Fusion — preserves input order as rank.

    Note: Omits ``query`` parameter intentionally. RRF is order-based and
    does not use query-document similarity, so passing a query would be
    misleading.
    """
    items = [
        RerankedItem(
            index=i,
            score=1.0 / (i + 1 + k),
            text=doc,
        )
        for i, doc in enumerate(documents)
    ]
    return RerankResult(items=items[:top_k], backend="RRF", latency_ms=0.0)


# ---------------------------------------------------------------------------
# Public API — try CrossEncoder, fall back to RRF
# ---------------------------------------------------------------------------
def rerank(
    query: str,
    documents: list[str],
    top_k: int = 5,
) -> RerankResult:
    """Rerank documents. Uses CrossEncoder if available, else RRF."""
    result = rerank_cross_encoder(query, documents, top_k=top_k)
    if result is not None:
        return result

    logger.warning("reranker_fallback_activated")
    return rerank_rrf(documents, top_k=top_k)
