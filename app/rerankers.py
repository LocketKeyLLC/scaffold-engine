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
# §17.187 / §17.1124 — reranker score normalisation: ONE rule.
#
# Every CrossEncoder this engine loads is a single-logit classifier. The engine
# takes the model's RAW logit (``predict(..., activation_fn=Identity)``, see
# ``predict_raw``) and squashes it with a sigmoid ONCE, here — so the
# downstream confidence threshold (``settings.confidence_threshold``, on
# [0, 1]) survives a MODEL_RERANKER swap without per-deployment retuning.
#
# Why the previous per-model registry (identity for Qwen3, sigmoid for
# ms-marco / bge) was wrong under sentence-transformers ≥ 3: ``predict``
# applies a per-model DEFAULT activation before our normaliser ran — Sigmoid
# for Qwen3 / bge / gte, Identity for ms-marco. "Qwen3 is already-sigmoid" was
# that library default, not the model; bge got sigmoid TWICE (raw 7.99 → 1.0 →
# 0.73, so its best hit could never clear a 0.8 threshold). Asking for the raw
# logit removes the dependency on the library default entirely.
#
# The family list survives only as the /health label: a name outside it is
# still sigmoided (the safe default for a single-logit reranker) but reported
# as an unregistered family so an operator sees the gap.
#
# MODEL_RERANKER is config-only per the project invariants (cannot be swapped
# per-request), so both the normaliser and the pair template are selected
# once at the call site against ``settings.model_reranker``. Match is
# substring-on-lowercased name so the common variants (org prefix, version
# suffix) match without enumerating every published tag.
# ---------------------------------------------------------------------------
def _normalize_identity(scores: list[float]) -> list[float]:
    return list(scores)


def _normalize_sigmoid(scores: list[float]) -> list[float]:
    return [1.0 / (1.0 + math.exp(-float(s))) for s in scores]


#: Reranker families this engine has measured (per-pair cost + golden-set
#: quality, §17.1124). Membership only changes the /health label.
_KNOWN_RERANKER_FAMILIES: tuple[str, ...] = (
    "qwen3-reranker", "ms-marco", "bge-reranker", "gte-reranker",
)
_RAW_LOGIT_LABEL = "raw logit → sigmoid"

#: Families whose training template wraps each pair in an instruct prompt
#: (``reranker_prompt_system`` / ``_suffix`` / ``_default_instruction``).
#: Every other family scores the plain (query, document) pair — sending a
#: BERT-style cross-encoder the Qwen chat template is noise it was never
#: trained on, and it costs ~60 tokens per pair (§17.1124).
_INSTRUCT_TEMPLATE_FAMILIES: tuple[str, ...] = ("qwen3-reranker",)


def get_score_range_info(
    model_name: str | None,
) -> tuple[str, Callable[[list[float]], list[float]]]:
    """Return ``(range_label, normalizer)`` for the configured reranker.

    Every model is scored as a raw logit and sigmoided (see the module note);
    the label tells /health whether the family is one this engine has
    measured. ``None`` (no model configured) is distinct from an unknown
    name so an operator can tell config-missing from config-unrecognised.
    """
    if not model_name:
        return ("unknown (no model configured)", _normalize_identity)
    name = model_name.lower()
    if any(fam in name for fam in _KNOWN_RERANKER_FAMILIES):
        return (_RAW_LOGIT_LABEL, _normalize_sigmoid)
    return (f"unknown family (assumed {_RAW_LOGIT_LABEL})", _normalize_sigmoid)


def uses_instruct_template(model_name: str | None) -> bool:
    """True when the configured reranker expects the instruct-wrapped pair."""
    name = (model_name or "").lower()
    return any(fam in name for fam in _INSTRUCT_TEMPLATE_FAMILIES)


def build_pairs(
    query: str, documents: list[str], model_name: str | None = None,
) -> list[list[str]]:
    """The ONE place a (query, document) pair is shaped for the reranker —
    shared by the in-process path and the HTTP/sidecar path so the two can
    never drift (§17.1124). Instruct families get the template; the rest get
    the plain pair."""
    from app.config import settings
    name = settings.model_reranker if model_name is None else model_name
    if uses_instruct_template(name):
        return [[_format_query(query), _format_document(d)] for d in documents]
    return [[query, d] for d in documents]


def predict_raw(model, pairs: list[list[str]]):
    """Score pairs as RAW logits, whatever the model's default activation.

    sentence-transformers ≥ 3 picks a per-model default activation inside
    ``predict`` (Sigmoid for Qwen3/bge/gte, Identity for ms-marco); passing
    ``Identity`` explicitly is what makes the single sigmoid in
    ``get_score_range_info`` correct for every family. No fallback on
    purpose: a ``predict`` that rejects the kwarg would silently reintroduce
    the double sigmoid, and the pinned sentence-transformers accepts it.
    """
    import torch
    return model.predict(pairs, activation_fn=torch.nn.Identity())


# ---------------------------------------------------------------------------
# Lazy-loaded CrossEncoder singleton
# ---------------------------------------------------------------------------
_cross_encoder = None
_load_failed = False
_load_failed_at = 0.0  # monotonic time of the last hard load failure
# §17.812 (audit C4) — retry a hard-failed load after this cooldown so a transient
# failure at boot doesn't pin every query to RRF-only for the process lifetime.
_LOAD_RETRY_COOLDOWN_S = 300.0
_load_lock = threading.Lock()


def reranker_load_failed() -> bool:
    """§17.812 (audit C4) — True while the CrossEncoder load has HARD-FAILED (RAG
    is on RRF-only fallback). /health reads this so a dead reranker reports
    'down' instead of the stale prewarm 'up' — the prewarm catches no exception
    when the load merely returns None, so it would otherwise stamp 'up'."""
    return _load_failed


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
    global _cross_encoder, _load_failed, _load_failed_at
    # Fast path (no lock) — hot path after initial load
    if _cross_encoder is not None:
        return _cross_encoder
    if _load_failed:
        # §17.812 (audit C4) — self-heal: the failure was STICKY for the whole
        # process (reset_reranker had no caller). After the cooldown, clear the
        # flag and let this call retry the load instead of staying dead forever.
        if (time.monotonic() - _load_failed_at) < _LOAD_RETRY_COOLDOWN_S:
            return None
        with _load_lock:
            if _load_failed and (time.monotonic() - _load_failed_at) >= _LOAD_RETRY_COOLDOWN_S:
                logger.info(
                    "crossencoder_retry_after_cooldown: down_for_s=%.0f",
                    time.monotonic() - _load_failed_at,
                )
                _load_failed = False

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
        _load_failed_at = time.monotonic()  # §17.812 — start the retry cooldown
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
    pairs = build_pairs(query, docs)

    try:
        from app.config import settings
        t0 = time.monotonic()
        raw_scores = predict_raw(model, pairs)
        # §17.187/§17.1124 — raw logit → one sigmoid, so threshold semantics
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
def rerank_http(
    query: str,
    documents: list[str],
    top_k: int = 5,
    max_pairs: int = _MAX_PAIRS,
) -> RerankResult | None:
    """§17.1065 — score via a sidecar speaking the text-embeddings-inference
    ``POST /rerank`` API (the bundled ``app.reranker_service`` or a real TEI
    for a model it supports). Same pair formatting and the same [0, 1]
    normalisation as the in-process path, so a backend swap does not move
    the confidence threshold. Returns None on any failure (the caller falls
    back)."""
    import httpx
    from app.config import settings
    docs = documents[:max_pairs]
    if not docs:
        return RerankResult(items=[], backend="HTTP", latency_ms=0.0)
    pairs = build_pairs(query, docs)
    payload = {
        "query": pairs[0][0],
        "texts": [p[1] for p in pairs],
        "raw_scores": True,
        "truncate": True,
    }
    try:
        t0 = time.monotonic()
        with httpx.Client(timeout=settings.reranker_timeout_s) as client:
            r = client.post(f"{settings.reranker_url.rstrip('/')}/rerank", json=payload)
        r.raise_for_status()
        rows = r.json()
        elapsed_ms = (time.monotonic() - t0) * 1000
        raw = [0.0] * len(docs)
        for row in rows:
            raw[int(row["index"])] = float(row["score"])
        _, normalize = get_score_range_info(settings.model_reranker)
        scores = normalize(raw)
        items = [RerankedItem(index=i, score=float(s), text=docs[i]) for i, s in enumerate(scores)]
        items.sort(key=lambda x: x.score, reverse=True)
        items = items[:top_k]
        logger.info("reranker_completed: backend=HTTP docs=%d elapsed_ms=%.0f top_score=%.4f",
                    len(docs), elapsed_ms, items[0].score if items else 0)
        return RerankResult(items=items, backend="HTTP", latency_ms=elapsed_ms)
    except Exception as e:  # noqa: BLE001 — a sidecar hiccup must degrade, never raise
        logger.warning("http_rerank_failed: url=%s error=%s (falling back)", settings.reranker_url, e)
        return None


def rerank(
    query: str,
    documents: list[str],
    top_k: int = 5,
    max_pairs: int = _MAX_PAIRS,
) -> RerankResult:
    """Rerank documents. Uses CrossEncoder if available, else RRF.

    §17.608 — ``max_pairs`` is now plumbed through so callers that have
    already bounded their shortlist (the RAG pipeline caps at
    ``settings.rerank_max_candidates``, ge=1 le=512) can have every
    shortlisted candidate scored instead of being silently truncated to
    the ``_MAX_PAIRS`` default. Bare callers keep the safe default.
    """
    from app.config import settings
    if (settings.reranker_backend or "local").lower() in ("http", "tei"):  # §17.1065
        result = rerank_http(query, documents, top_k=top_k, max_pairs=max_pairs)
        if result is not None:
            return result
    result = rerank_cross_encoder(query, documents, top_k=top_k, max_pairs=max_pairs)
    if result is not None:
        return result

    logger.warning("reranker_fallback_activated")
    return rerank_rrf(documents, top_k=top_k)
