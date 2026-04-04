"""rerankers.py — CrossEncoder reranker with RRF fallback."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy-loaded CrossEncoder singleton
# ---------------------------------------------------------------------------
_cross_encoder = None
_load_failed = False


def _get_cross_encoder():
    """Load model once, on first call. Returns None if unavailable."""
    global _cross_encoder, _load_failed
    if _load_failed:
        return None
    if _cross_encoder is not None:
        return _cross_encoder
    try:
        from sentence_transformers import CrossEncoder
        model_name = "tomaarsen/Qwen3-Reranker-0.6B-seq-cls"
        logger.info("crossencoder_loading: model=%s", model_name)
        t0 = time.monotonic()
        _cross_encoder = CrossEncoder(model_name, trust_remote_code=True)
        elapsed = time.monotonic() - t0
        logger.info("crossencoder_loaded: elapsed_s=%.1f", elapsed)
        return _cross_encoder
    except Exception as e:
        _load_failed = True
        logger.error("crossencoder_load_failed: error=%s", e)
        return None




# ---------------------------------------------------------------------------
# Qwen3-Reranker prompt template (required for score calibration)
# ---------------------------------------------------------------------------
_RERANKER_SYSTEM = (
    "<|im_start|>system\n"
    "Judge whether the Document meets the requirements based on the Query "
    "and the Instruct provided. Note that the answer can only be \"yes\" "
    "or \"no\".<|im_end|>\n<|im_start|>user\n"
)
_RERANKER_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
_DEFAULT_INSTRUCTION = "Given a web search query, retrieve relevant passages that answer the query"


def _format_query(query: str, instruction: str | None = None) -> str:
    inst = instruction or _DEFAULT_INSTRUCTION
    return f"{_RERANKER_SYSTEM}<Instruct>: {inst}\n<Query>: {query}\n"


def _format_document(document: str) -> str:
    return f"<Document>: {document}{_RERANKER_SUFFIX}"

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
    max_pairs: int = 20,
) -> RerankResult | None:
    """Score query-document pairs via CrossEncoder. Returns None on failure."""
    model = _get_cross_encoder()
    if model is None:
        return None

    docs = documents[:max_pairs]
    pairs = [[_format_query(query), _format_document(doc)] for doc in docs]

    try:
        t0 = time.monotonic()
        scores = model.predict(pairs)
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
    """Reciprocal Rank Fusion — preserves input order as rank."""
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
