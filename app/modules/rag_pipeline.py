"""Scaffold Engine -- RAG pipeline module.

#118 Canonical field names (schema migration in progress):
  - canonical_text  (legacy alias: content)
  - title           (legacy alias: topic)
  - source_url      (legacy alias: source)
  - domain_tags     (legacy alias: tags)

#116 Milvus expression safety:
  pymilvus expr= accepts interpolated literals, not bind params.
  Keyword terms are restricted to ASCII alphanumeric (strips LIKE
  wildcards % _, backslash, quotes). Domain strings have " and \\
  escaped. Do not add quotes around int/bool values.

Domain contract (no silent defaults):
  - domain=None   → no partition filter (searches all partitions)
  - domain=""     → ValueError
  - domain="eng"  → filter to that partition

Query flow:
  1. Embed query
  2. Vector ANN + keyword search in parallel
  3. RRF fusion (dataclasses.replace — no mutation)
  4. Cross-encoder rerank (with empty-items fallback)
  5. Confidence filter + dynamic top-k
  6. Post-query supersedes sweep (drops superseded ancestors)
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import Any, TYPE_CHECKING

from pymilvus import MilvusClient

if TYPE_CHECKING:
    from app.modules._rag_protocol import IngestStatsDict, RagResponseDict
from app.utils.milvus_utils import get_client
from app.utils.rag_result_cache import get_rag_result_cache
from app.rerankers import rerank as cross_encoder_rerank

from app import model_router
from app.config import settings, VALID_DOMAINS
from app.utils.staleness import compute_expires_at
from app.utils.embedding_cache import get_cache, truncate_and_normalize, normalize_cache_text
from app.modules.provenance import (
    confidence_for,
    get_provenance_batch,
    write_provenance_batch,
)

from sqlalchemy import text
from app.database import async_session

logger = logging.getLogger("scaffold.rag")

COLLECTION_NAME = "toon_v2"
EMBED_DIM = 512
DEFAULT_TOP_K = 10
MAX_TOP_K = 100  # #119
CONFIDENCE_THRESHOLD = 0.8
RRF_K = 60

# Reranker tuning lives in app.config.settings (rerank_max_candidates, etc.)

# Max hops for version-chain walk-forward (cycle protection).
_VERSION_CHAIN_WALK_MAX = 8

# #109/#111
KEYWORD_MAX_TERMS = 5
_KEYWORD_TERM_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset({
    "the", "and", "for", "are", "but", "not", "you", "all", "can",
    "has", "was", "one", "our", "out", "how", "what", "when",
    "where", "which", "who", "why", "with", "this", "that", "from", "into",
})


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------

@dataclass
class RagResult:
    content: str = ""
    title: str = ""
    tags: str = ""
    source_url: str = ""
    entry_id: str = ""
    domain: str = ""
    vector_score: float = 0.0
    keyword_score: float = 0.0
    rrf_score: float = 0.0
    rerank_score: float = 0.0
    final_score: float = 0.0
    version: int = 1
    supersedes_id: str = ""
    confidence_score: float = 0.0
    source_type: str = ""


# §17.591 — `collection` locals below now hold a MilvusClient (see get_client);
# the toon_v2 name is passed per-call as collection_name=COLLECTION_NAME.
_get_client = get_client


# ---------------------------------------------------------------------------
# Domain expression helper
# ---------------------------------------------------------------------------

def _escape_literal(s: str) -> str:
    """Escape \\ and \" for safe interpolation into Milvus expr literals."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _normalize_entry(entry: dict) -> dict:
    """Map an entry dict's loose alias keys onto the canonical names.

    Delegates to the typed ``IngestEntry`` model in ``_rag_entry.py``.
    The model centralizes the TOON↔Milvus dual-name conversion as the
    single source of truth — TOON LLM outputs use short keys
    (``topic``, ``content``, ``tags``, ``source``); Milvus storage uses
    long keys (``canonical_text``, ``domain_tags``, ``source_url``);
    the model accepts both with first-non-empty-wins semantics matching
    this function's prior behavior. Audit item 6.

    Returned shape (canonical names) — caller may treat as a typed dict:
        content (str), title (str), domain_tags (list[str]), source_url
        (str), source_type (str), confidence (float).
    """
    from app.modules._rag_entry import IngestEntry
    return IngestEntry.from_input(entry).to_canonical_dict()


def _domain_expr(domain: str | None) -> str | None:
    """Translate a single-domain arg → Milvus expr clause.

    None → None  (caller is responsible for fan-out across VALID_DOMAINS)
    ""   → ValueError (no silent default)
    else → 'domain == "<escaped>"'
    """
    if domain == "":
        raise ValueError('domain="" is not allowed; pass None to search all partitions')
    if domain is None:
        return None
    return f'domain == "{_escape_literal(domain)}"'


def _iter_search_domains(
    domain: str | None,
    *,
    hint: str | None = None,
) -> list[str]:
    """Expand a user-supplied domain arg into the list of domains to fan out to.

    Milvus partition-key isolation rejects both "no expr" and "IN" exprs, so a
    caller asking for domain=None is served by running one == search per
    configured partition and merging the results.

    §17.188 — ``hint`` is a softer alternative to ``domain``: when ``domain``
    is None AND a valid ``hint`` is provided, fan out only to
    ``{hint, "llm"}`` instead of every partition. The audit (AUDIT.md 2.6)
    flagged the all-partitions fan-out as a scaling concern as VALID_DOMAINS
    grows; the hint path keeps a small cross-domain fallback open without
    paying the full N-search cost. Has no effect when ``domain`` is set
    (strict mode wins). An invalid hint is logged + ignored — falls through
    to the full-fan-out fallback rather than throwing, so a typo never
    breaks retrieval.
    """
    if domain == "":
        raise ValueError('domain="" is not allowed; pass None to search all partitions')
    if domain is not None:
        return [domain]
    if hint is not None:
        if hint not in VALID_DOMAINS:
            logger.warning(
                "invalid_domain_hint_ignored: hint=%r valid=%s; "
                "falling back to all-partition fan-out",
                hint, sorted(VALID_DOMAINS),
            )
            return sorted(VALID_DOMAINS)
        # Always include "llm" as the generic-knowledge fallback so a
        # cross-domain hit still surfaces. When the hint IS "llm" the
        # set collapses to just ["llm"] — no duplicate work.
        return sorted({hint, "llm"})
    return sorted(VALID_DOMAINS)


# ---------------------------------------------------------------------------
# Embedding helpers
# ---------------------------------------------------------------------------

async def _embed_query(
    query: str, *, query_intent: str = "general",
) -> list[float] | None:
    """Embed query — delegates to app.utils.embedding.embed_query.

    §17.767 (Phase 2) — fail LOUD on a dimension mismatch. MRL truncation
    (`truncate_and_normalize`) slices to the first `embedding_dim` values with no
    padding, so an embedder producing FEWER than `embedding_dim` dims yields a
    short vector that Milvus then silently rejects inside the per-partition search
    (→ 0 results, status 'ok'). Detect it here and return None so `query_rag`
    takes its existing embed-failure path (status='error') instead of a misleading
    empty success. The production embedder is 768-d (slices cleanly ≥ 512), so
    this never fires on the current config — it only converts a future silent-fail
    into a loud, correct error.
    """
    from app.utils.embedding import embed_query as _public_embed_query
    vec = await _public_embed_query(query, query_intent=query_intent)
    if vec is not None and len(vec) < settings.embedding_dim:
        logger.error(
            "embed_dim_mismatch: query embedding has %d dims, expected %d "
            "(embedder misconfigured?) — treating as embed failure",
            len(vec), settings.embedding_dim,
        )
        return None
    return vec


async def _embed_content(content: str) -> list[float] | None:
    """Embed content (no instruction prefix), MRL truncation, cache."""
    cache = get_cache()
    cached = await cache.get(content)
    if cached:
        return cached

    embeddings = await model_router.embed(content, role="model_embedder_pipeline")
    if not embeddings or not embeddings[0]:
        return None

    truncated = truncate_and_normalize(embeddings[0])
    await cache.put(content, truncated)
    return truncated


async def _embed_contents_batch(texts: list[str]) -> list[list[float] | None]:
    """Batch embed texts, honoring cache. Falls back to serial if model_router
    rejects list input or returns mismatched length.
    """
    cache = get_cache()
    out: list[list[float] | None] = [None] * len(texts)
    misses: list[tuple[int, str]] = []

    for i, t in enumerate(texts):
        if not t:
            continue
        hit = await cache.get(t)
        if hit is not None:
            out[i] = hit
        else:
            misses.append((i, t))

    batch = max(1, int(settings.embedding_batch_size))
    for start in range(0, len(misses), batch):
        chunk = misses[start : start + batch]
        chunk_texts = [t for _, t in chunk]
        embs = None
        try:
            embs = await model_router.embed(
                chunk_texts, role="model_embedder_pipeline",
            )
        except Exception as e:
            logger.info("batch embed not supported or failed (%s); falling back to serial", e)

        if embs and len(embs) == len(chunk):
            for (idx, txt), vec in zip(chunk, embs):
                if not vec:
                    continue
                truncated = truncate_and_normalize(vec)
                await cache.put(txt, truncated)
                out[idx] = truncated
        else:
            for idx, txt in chunk:
                try:
                    ev = await model_router.embed(txt, role="model_embedder_pipeline")
                    if ev and ev[0]:
                        truncated = truncate_and_normalize(ev[0])
                        await cache.put(txt, truncated)
                        out[idx] = truncated
                except Exception as e:
                    logger.warning("serial embed failed for idx=%d: %s", idx, e)

    return out


# ---------------------------------------------------------------------------
# Vector search
# ---------------------------------------------------------------------------

async def _vector_search(
    collection: "MilvusClient",
    query_embedding: list[float],
    top_k: int,
    domain: str | None = None,
    *,
    domain_hint: str | None = None,
) -> tuple[list[RagResult], list[str]]:
    """ANN search in Milvus (off event loop).

    Fans out one search per partition when domain is None. Under partition-key
    isolation Milvus rejects both "no expr" and "IN" over the partition key,
    so per-partition == exprs are the only safe path.

    §17.188 — ``domain_hint`` narrows the fan-out from "all partitions" to
    ``{hint, "llm"}`` when ``domain`` is None. See ``_iter_search_domains``.
    """
    domains = _iter_search_domains(domain, hint=domain_hint)

    def _search_one(d: str) -> tuple[list[RagResult], str | None]:
        # §17.542 — one partition per executor thread so the fan-out runs
        # concurrently (vector+keyword legs already prove same-Collection
        # concurrent search is safe; this is more of the same). Per-domain
        # try/except isolates a failing partition without aborting the rest.
        # §17.767 — return the domain name when the search RAISED (not just an
        # empty hit list) so query_rag can flag degraded/partial retrieval.
        if collection is None:
            return [], None
        hits: list[RagResult] = []
        try:
            search_kwargs: dict[str, Any] = dict(
                collection_name=COLLECTION_NAME,
                data=[query_embedding],
                anns_field="dense_vector",
                search_params={"metric_type": "COSINE", "params": {"ef": 128, "refine_k": 2}},
                limit=top_k,
                filter=f'domain == "{_escape_literal(d)}"',
                output_fields=[
                    "canonical_text", "title", "domain_tags", "source_url",
                    "entry_id", "domain", "confidence_score", "version",
                    "supersedes_id", "source_type",
                ],
            )
            results = collection.search(**search_kwargs)
            for hit in results[0]:
                entity = hit["entity"]
                tags_list = entity.get("domain_tags", [])
                tags_str = ", ".join(tags_list) if isinstance(tags_list, list) else str(tags_list)
                hits.append(RagResult(
                    content=entity.get("canonical_text", ""),
                    title=entity.get("title", ""),
                    tags=tags_str,
                    source_url=entity.get("source_url", ""),
                    entry_id=entity.get("entry_id", ""),
                    domain=entity.get("domain", ""),
                    vector_score=float(hit["distance"]),
                    version=entity.get("version", 1),
                    supersedes_id=entity.get("supersedes_id", ""),
                    confidence_score=float(entity.get("confidence_score", 0.0) or 0.0),
                    source_type=entity.get("source_type", ""),
                ))
        except Exception as e:
            logger.warning("Vector search failed (domain=%s): %s", d, e)
            return hits, d
        return hits, None

    if collection is None:
        return [], []
    loop = asyncio.get_running_loop()
    per_domain = await asyncio.gather(
        *[loop.run_in_executor(None, _search_one, d) for d in domains]
    )
    # Merge across partitions: sort by score desc, keep top_k. §17.767 — collect
    # partitions whose search RAISED so query_rag distinguishes a true-empty
    # result from a degraded (partial/total-failure) one.
    all_hits = [h for hits, _ in per_domain for h in hits]
    failed = [d for _, d in per_domain if d is not None]
    all_hits.sort(key=lambda r: r.vector_score, reverse=True)
    return all_hits[:top_k], failed


# ---------------------------------------------------------------------------
# Keyword search — §17.431 BM25 sparse (preferred) with LIKE-scan fallback
# ---------------------------------------------------------------------------

# §17.597 — cache BM25-field presence per client so the keyword-search
# dispatcher doesn't fire a synchronous describe_collection gRPC on the event
# loop for every query. Presence only changes via a schema migration (which
# requires a restart), so a per-client memo is safe. Keyed by id(client);
# tests clear it via the routed fixture.
_bm25_present_cache: dict[int, bool] = {}


async def _collection_has_bm25_cached(collection: "MilvusClient") -> bool:
    key = id(collection)
    cached = _bm25_present_cache.get(key)
    if cached is None:
        from app.utils.milvus_utils import collection_has_bm25
        # Off-loop: describe_collection is a blocking PyMilvus gRPC call.
        cached = await asyncio.to_thread(collection_has_bm25, collection)
        _bm25_present_cache[key] = cached
    return cached


async def _keyword_search(
    collection: "MilvusClient",
    query: str,
    top_k: int,
    domain: str | None = None,
    *,
    domain_hint: str | None = None,
) -> tuple[list[RagResult], list[str]]:
    """Dispatch the hybrid keyword leg: Milvus BM25 sparse search when enabled
    AND the collection is migrated (has the sparse field), else the substring
    LIKE scan. Both return RagResult lists with keyword_score set; the score
    SCALE differs but _rrf_fuse is rank-based so fusion is unaffected (§17.431).
    """
    if (
        settings.rag_bm25_enabled
        and collection is not None
        and await _collection_has_bm25_cached(collection)
    ):
        return await _bm25_search(
            collection, query, top_k, domain, domain_hint=domain_hint,
        )
    return await _keyword_search_like(
        collection, query, top_k, domain, domain_hint=domain_hint,
    )


async def _bm25_search(
    collection: "MilvusClient",
    query: str,
    top_k: int,
    domain: str | None = None,
    *,
    domain_hint: str | None = None,
) -> tuple[list[RagResult], list[str]]:
    """Milvus 2.5 native BM25 sparse search (off event loop).

    Queries the ``sparse_bm25`` field with the RAW query text — Milvus
    tokenizes + scores via the BM25 Function. Per-partition fan-out mirrors
    _vector_search (partition-key isolation rejects IN / unfiltered exprs).
    keyword_score = BM25 relevance (higher = better); feeds the rank-based RRF.
    """
    from app.utils.milvus_utils import BM25_SPARSE_FIELD

    if not (query or "").strip():
        return [], []
    domains = _iter_search_domains(domain, hint=domain_hint)

    def _search_one(d: str) -> tuple[list[RagResult], str | None]:
        # §17.542 — per-partition executor fan-out (see _vector_search).
        # §17.767 — signal a RAISED partition (see _vector_search).
        if collection is None:
            return [], None
        hits: list[RagResult] = []
        try:
            results = collection.search(
                collection_name=COLLECTION_NAME,
                data=[query],
                anns_field=BM25_SPARSE_FIELD,
                search_params={"metric_type": "BM25"},
                limit=top_k,
                filter=f'domain == "{_escape_literal(d)}"',
                output_fields=[
                    "canonical_text", "title", "domain_tags", "source_url",
                    "entry_id", "domain", "version", "supersedes_id",
                    "confidence_score", "source_type",
                ],
            )
            for hit in results[0]:
                entity = hit["entity"]
                tags_list = entity.get("domain_tags", [])
                tags_str = ", ".join(tags_list) if isinstance(tags_list, list) else str(tags_list)
                hits.append(RagResult(
                    content=entity.get("canonical_text", ""),
                    title=entity.get("title", ""),
                    tags=tags_str,
                    source_url=entity.get("source_url", ""),
                    entry_id=entity.get("entry_id", ""),
                    domain=entity.get("domain", ""),
                    keyword_score=float(hit["distance"]),
                    version=entity.get("version", 1),
                    supersedes_id=entity.get("supersedes_id", ""),
                    confidence_score=float(entity.get("confidence_score", 0.0) or 0.0),
                    source_type=entity.get("source_type", ""),
                ))
        except Exception as e:
            logger.warning("BM25 search failed (domain=%s): %s", d, e)
            return hits, d
        return hits, None

    if collection is None:
        return [], []
    loop = asyncio.get_running_loop()
    per_domain = await asyncio.gather(
        *[loop.run_in_executor(None, _search_one, d) for d in domains]
    )
    all_hits = [h for hits, _ in per_domain for h in hits]
    failed = [d for _, d in per_domain if d is not None]
    all_hits.sort(key=lambda r: r.keyword_score, reverse=True)
    return all_hits[:top_k], failed


async def _keyword_search_like(
    collection: "MilvusClient",
    query: str,
    top_k: int,
    domain: str | None = None,
    *,
    domain_hint: str | None = None,
) -> tuple[list[RagResult], list[str]]:
    """Keyword search via substring LIKE scan (off event loop) — the pre-§17.431
    fallback used when BM25 is disabled or the collection isn't migrated.

    Tokens are restricted to [a-z0-9]+ via _KEYWORD_TERM_RE — eliminates
    LIKE wildcards, escape chars, and quotes from the interpolation path.

    Fans out one query per partition when domain is None (partition-key
    isolation rejects IN and unfiltered exprs).

    §17.188 — ``domain_hint`` narrows fan-out; see ``_iter_search_domains``.
    """
    tokens = _KEYWORD_TERM_RE.findall(query.lower())
    words = [t for t in tokens if len(t) >= 3 and t not in _STOPWORDS]
    if not words:
        return [], []

    conditions: list[str] = []
    for word in words[:KEYWORD_MAX_TERMS]:
        conditions.append(f'canonical_text like "%{word}%"')
        conditions.append(f'title like "%{word}%"')
    keyword_expr = " or ".join(conditions)

    domains = _iter_search_domains(domain, hint=domain_hint)

    def _search_one(d: str) -> tuple[list[RagResult], str | None]:
        # §17.542 — per-partition executor fan-out (see _vector_search).
        # §17.767 — signal a RAISED partition (see _vector_search).
        if collection is None:
            return [], None
        hits: list[RagResult] = []
        expr = f'domain == "{_escape_literal(d)}" and ({keyword_expr})'
        try:
            results = collection.query(
                collection_name=COLLECTION_NAME,
                filter=expr,
                output_fields=[
                    "canonical_text", "title", "domain_tags", "source_url",
                    "entry_id", "domain", "version", "supersedes_id",
                    "confidence_score", "source_type",
                ],
                limit=top_k,
            )
            for r in results:
                content_lower = r.get("canonical_text", "").lower()
                title_lower = r.get("title", "").lower()
                match_count = sum(1 for w in words if w in content_lower or w in title_lower)
                score = match_count / len(words) if words else 0.0
                tags_list = r.get("domain_tags", [])
                tags_str = ", ".join(tags_list) if isinstance(tags_list, list) else str(tags_list)
                hits.append(RagResult(
                    content=r.get("canonical_text", ""),
                    title=r.get("title", ""),
                    tags=tags_str,
                    source_url=r.get("source_url", ""),
                    entry_id=r.get("entry_id", ""),
                    domain=r.get("domain", ""),
                    keyword_score=score,
                    version=r.get("version", 1),
                    supersedes_id=r.get("supersedes_id", ""),
                    confidence_score=float(r.get("confidence_score", 0.0) or 0.0),
                    source_type=r.get("source_type", ""),
                ))
        except Exception as e:
            logger.warning("Keyword search failed (domain=%s): %s", d, e)
            return hits, d
        return hits, None

    if collection is None:
        return [], []
    loop = asyncio.get_running_loop()
    per_domain = await asyncio.gather(
        *[loop.run_in_executor(None, _search_one, d) for d in domains]
    )
    all_hits = [h for hits, _ in per_domain for h in hits]
    failed = [d for _, d in per_domain if d is not None]
    all_hits.sort(key=lambda r: r.keyword_score, reverse=True)
    return all_hits[:top_k], failed


# ---------------------------------------------------------------------------
# RRF fusion
# ---------------------------------------------------------------------------

def _rrf_fuse(
    vector_results: list[RagResult],
    keyword_results: list[RagResult],
    k: int = RRF_K,
) -> list[RagResult]:
    """Reciprocal Rank Fusion.

    Uses dataclasses.replace for accumulator updates so upstream RagResult
    instances from vector_results / keyword_results are never mutated.
    """
    merged: dict[str, RagResult] = {}

    def _key(result: RagResult) -> str:
        # entry_id is the canonical identity. The content[:200] fallback
        # exists for the rare case where an upstream search returned a row
        # without entry_id; collisions are possible there (two distinct
        # rows sharing a 200-char prefix), but accepting that occasional
        # over-merge is preferable to dropping the result entirely.
        return result.entry_id or result.content[:200]

    for rank, result in enumerate(vector_results):
        key = _key(result)
        base = merged.get(key, result)
        merged[key] = replace(
            base,
            rrf_score=base.rrf_score + 1.0 / (k + rank + 1),
            vector_score=max(base.vector_score, result.vector_score),
        )

    sorted_kw = sorted(keyword_results, key=lambda r: r.keyword_score, reverse=True)
    for rank, result in enumerate(sorted_kw):
        key = _key(result)
        base = merged.get(key, result)
        merged[key] = replace(
            base,
            rrf_score=base.rrf_score + 1.0 / (k + rank + 1),
            keyword_score=max(base.keyword_score, result.keyword_score),
        )

    return sorted(merged.values(), key=lambda r: r.rrf_score, reverse=True)


# ---------------------------------------------------------------------------
# Cross-encoder reranking
# ---------------------------------------------------------------------------

async def _rerank(
    query: str,
    results: list[RagResult],
    top_k: int,
    *,
    max_candidates: int | None = None,
    doc_truncate: int | None = None,
) -> tuple[list[RagResult], dict[str, Any]]:
    """Rerank via CrossEncoder. Returns (ranked, meta).

    meta contains: backend, skipped_rerank, warnings (list[str]).

    §17.234 — ``max_candidates`` overrides ``settings.rerank_max_candidates``
    per call. When None, the config default applies (post-§17.233: 10).
    §17.252 — ``doc_truncate`` overrides ``settings.rerank_doc_truncate``
    per call. When None, the config default applies (post-§17.235: 500).
    """
    meta: dict[str, Any] = {"backend": None, "skipped_rerank": False, "warnings": []}

    if not results:
        return [], meta

    max_cand = int(max_candidates if max_candidates is not None else settings.rerank_max_candidates)
    doc_trunc = int(doc_truncate if doc_truncate is not None else settings.rerank_doc_truncate)
    warn_ms = int(settings.rerank_warn_ms)
    error_ms = int(settings.rerank_error_ms)

    docs = [r.content[:doc_trunc] for r in results[:max_cand]]

    loop = asyncio.get_running_loop()
    # §17.608 — pass max_pairs=len(docs) so the CrossEncoder scores the WHOLE
    # shortlist we just built. Previously the reranker used its internal
    # _MAX_PAIRS=20 default, so any max_cand > 20 was silently truncated to 20
    # items; the len(rr.items) < len(docs) guard below then misread that healthy
    # truncation as a partial-failure and disabled reranking entirely (rebuilding
    # every result on the RRF scale) while still paying the CrossEncoder cost.
    # The config/schema le=512 bound on rerank_max_candidates is the authoritative
    # ceiling; the reranker now honors it end-to-end.
    rr = await loop.run_in_executor(None, cross_encoder_rerank, query, docs, len(docs), len(docs))

    meta["backend"] = getattr(rr, "backend", None)

    # Reranker contract (post-§17.260): for N docs in, return exactly N
    # items out. Both anomalies are unrecoverable for a stable sort:
    #   - items == [] : reranker silently produced nothing.
    #   - 0 < len(items) < len(docs) : partial result. Indices not in
    #     score_map would fall back to rrf_score, mixing two score scales
    #     in the final sort → undefined order. Bug found in §17.258 audit.
    # Recovery for both: rebuild every result with rerank_score = rrf_score
    # and sort by that — guarantees single-scale ordering.
    # Uses dataclasses.replace to honor _rrf_fuse's no-mutation contract:
    # callers may hold references to results that pre-date this rerank.
    if docs and len(rr.items) < len(docs):
        warning_kind = (
            "reranker_returned_no_items" if not rr.items
            else f"reranker_returned_partial_{len(rr.items)}_of_{len(docs)}"
        )
        logger.warning(
            "rerank_fallback: backend=%s returned %d items for %d docs (%s); "
            "falling back to RRF order to avoid mixed-scale sort",
            meta["backend"], len(rr.items), len(docs), warning_kind,
        )
        meta["skipped_rerank"] = True
        meta["warnings"].append(warning_kind)
        rebuilt = [replace(r, rerank_score=r.rrf_score, final_score=r.rrf_score) for r in results]
        rebuilt.sort(key=lambda r: r.final_score, reverse=True)
        return rebuilt[:top_k], meta

    score_map = {item.index: item.score for item in rr.items}
    rebuilt: list[RagResult] = []
    for i, r in enumerate(results[:max_cand]):
        score = score_map.get(i, r.rrf_score)
        rebuilt.append(replace(r, rerank_score=score, final_score=score))
    for r in results[max_cand:]:
        rebuilt.append(replace(r, rerank_score=r.rrf_score, final_score=r.rrf_score))
    results = rebuilt

    scores = [item.score for item in rr.items]
    _log_reranker = (
        logger.error if rr.latency_ms > error_ms
        else logger.warning if rr.latency_ms > warn_ms
        else logger.info
    )
    _log_reranker(
        "reranker_decision",
        extra=dict(
            query=query[:200],
            backend=rr.backend,
            n_candidates=len(docs),
            top_score=round(max(scores), 4) if scores else 0.0,
            min_score=round(min(scores), 4) if scores else 0.0,
            score_spread=round(max(scores) - min(scores), 4) if scores else 0.0,
            latency_ms=round(rr.latency_ms, 1),
            # §17.254 — log the EFFECTIVE knob values (per-call override
            # or settings fallback) so an operator grepping journald
            # for `reranker_decision` sees what shortlist depth + doc
            # truncate the call ran at, without parsing the per-result
            # JSON payload. The §17.253 metadata echo serves the
            # response-side caller; these two fields serve the
            # operator-side log-tailer.
            rerank_max_candidates=max_cand,
            rerank_doc_truncate=doc_trunc,
        ),
    )
    # §17.256 — Prometheus histogram keyed on the same effective knobs
    # the log line above carries. Same hot-path-safe shape as
    # record_llm_call / record_http_request: never raises.
    from app.observability.metrics import record_reranker_call
    record_reranker_call(
        max_candidates=max_cand,
        doc_truncate=doc_trunc,
        latency_ms=rr.latency_ms,
    )

    results.sort(key=lambda r: r.final_score, reverse=True)
    return results[:top_k], meta


# ---------------------------------------------------------------------------
# Supersedes sweep
# ---------------------------------------------------------------------------

async def _lookup_superseded(
    collection: "MilvusClient", entry_ids: list[str]
) -> set[str]:
    """Return the subset of entry_ids that are superseded by some other row.

    Queries: supersedes_id IN (entry_ids). A hit means some newer row points
    at one of our ids → that id is stale. Closes overview issue #7.

    §17.188 — limit is capped at ``settings.max_supersedes_lookup_results``
    (default 128) so a brief-flood scenario can't unboundedly inflate the
    Milvus query. When the cap fires a structured log line surfaces so an
    operator can decide whether to raise the cap.
    """
    if not entry_ids:
        return set()

    quoted = [f'"{_escape_literal(eid)}"' for eid in entry_ids]
    expr = f"supersedes_id in [{', '.join(quoted)}]"

    proposed_limit = max(1, len(entry_ids) * 4)
    effective_limit = min(proposed_limit, settings.max_supersedes_lookup_results)
    if proposed_limit > effective_limit:
        logger.warning(
            "supersedes_lookup_cap_fired: entry_ids=%d proposed_limit=%d "
            "effective_limit=%d (settings.max_supersedes_lookup_results)",
            len(entry_ids), proposed_limit, effective_limit,
        )

    def _sync() -> set[str]:
        try:
            rows = collection.query(
                collection_name=COLLECTION_NAME,
                filter=expr,
                output_fields=["supersedes_id"],
                limit=effective_limit,
            )
            return {r.get("supersedes_id", "") for r in rows if r.get("supersedes_id")}
        except Exception as e:
            logger.warning("supersedes_lookup_failed: %s", e)
            return set()

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def query_rag(
    query: str,
    *,
    domain: str | None = None,
    domain_hint: str | None = None,
    top_k: int = DEFAULT_TOP_K,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
    skip_rerank: bool = False,
    include_history: bool = False,
    query_intent: str = "general",
    max_candidates: int | None = None,
    doc_truncate: int | None = None,
) -> "RagResponseDict":
    """Full RAG pipeline: embed → search → fuse → rerank → filter → supersede-sweep.

    ``query_intent`` selects the embedder instruction template (§17.118).
    See ``EMBED_QUERY_TEMPLATES`` in ``app/utils/embedding.py`` for the
    supported intents (``general`` / ``code`` / ``qa`` / ``paper``).

    §17.188 — ``domain`` is strict (None searches every partition; a string
    searches that one only). ``domain_hint`` is a softer signal applied
    only when ``domain is None``: search ``{hint, "llm"}`` instead of the
    full all-partition fan-out so a caller that knows its likely domain
    can opt out of the linear-in-VALID_DOMAINS round-trip cost while still
    getting the "llm" generic-knowledge fallback. Has no effect when
    ``domain`` is set; an invalid hint is logged + ignored.
    """
    top_k = max(1, min(top_k, MAX_TOP_K))
    t0 = time.monotonic()

    warnings: list[str] = []

    # Result-cache lookup. Skipped when settings.cache_rag_results is False.
    # On hit, the response is returned with metadata.cache_hit=True so
    # callers can distinguish cached from fresh retrievals (e.g. for
    # latency dashboards). The cached metadata.latency_ms is the ORIGINAL
    # retrieval's latency, not the cache-hit time — cache-hit time is sub-
    # millisecond and not interesting to track.
    rag_cache = get_rag_result_cache()
    cached = await rag_cache.get(
        query, domain, top_k, confidence_threshold,
        skip_rerank, include_history, query_intent,
        max_candidates=max_candidates,
        doc_truncate=doc_truncate,
        domain_hint=domain_hint,  # §17.604 — part of the cache key
    )
    if cached is not None:
        # §17.264 — defensive shallow copy. The current Redis-backed
        # rag_cache.get() round-trips through json.loads, so each call
        # yields a fresh dict and shared-ref leakage is impossible today.
        # Copying anyway locks in the no-shared-state invariant so a
        # future in-process LRU in front of Redis (or any caller that
        # passes the same dict through twice) can't leak cache_hit=True
        # into a sibling response. The metadata sub-dict is copied too —
        # we mutate one of its keys.
        response = dict(cached)
        response["metadata"] = dict(response.get("metadata") or {})
        response["metadata"]["cache_hit"] = True
        return response

    loop = asyncio.get_running_loop()
    collection = await loop.run_in_executor(None, _get_client)
    if collection is None:
        return {
            "status": "error",
            "error": f"Collection '{COLLECTION_NAME}' not available",
            "results": [],
            "metadata": {"warnings": ["collection_unavailable"], "reranker_backend": None},
        }

    query_embedding = await _embed_query(query, query_intent=query_intent)
    if query_embedding is None:
        return {
            "status": "error",
            "error": "Failed to generate query embedding",
            "results": [],
            "metadata": {"warnings": ["embed_failed"], "reranker_backend": None},
        }

    (vector_results, vec_failed), (keyword_results, kw_failed) = await asyncio.gather(
        _vector_search(
            collection, query_embedding, top_k * 2,
            domain=domain, domain_hint=domain_hint,
        ),
        _keyword_search(
            collection, query, top_k * 2,
            domain=domain, domain_hint=domain_hint,
        ),
    )
    # §17.767 — partitions whose search RAISED (not just returned empty).
    # Surfaced in metadata so callers can distinguish a true-empty result from a
    # degraded (partial/total-failure) one instead of reading 0 results as
    # "nothing exists". Additive: empty on the happy path.
    partitions_failed = sorted(set(vec_failed) | set(kw_failed))
    if partitions_failed:
        warnings.append("partition_search_failed")

    logger.info(
        "search_executed: vector_hits=%d keyword_hits=%d query='%s'",
        len(vector_results), len(keyword_results), query[:50],
    )

    fused = _rrf_fuse(vector_results, keyword_results)

    rerank_meta: dict[str, Any] = {"skipped_rerank": False, "backend": None, "warnings": []}
    if skip_rerank or not fused:
        # §17.409 (arch-review R6) — use replace() for uniformity with the rest
        # of this module's immutable-RagResult discipline (these are fresh
        # _rrf_fuse objects, so in-place was already safe; this is consistency).
        fused = [replace(r, final_score=r.rrf_score) for r in fused]
        ranked = fused[:top_k]
        if skip_rerank:
            rerank_meta["skipped_rerank"] = True
    else:
        ranked, rerank_meta = await _rerank(query, fused, top_k, max_candidates=max_candidates, doc_truncate=doc_truncate)

    warnings.extend(rerank_meta.get("warnings", []))
    backend = rerank_meta.get("backend")
    skipped_rerank = rerank_meta.get("skipped_rerank", False)

    below_threshold = False
    fell_back_to_top3 = False
    if skip_rerank or skipped_rerank or confidence_threshold <= 0.0:
        filtered = ranked
    else:
        filtered = [r for r in ranked if r.final_score >= confidence_threshold]
        if len(filtered) == 0 and len(ranked) > 0:
            below_threshold = True
            fell_back_to_top3 = True
            filtered = ranked[:min(3, top_k)]
            warnings.append("below_threshold")
            warnings.append("fell_back_to_top3")

    # Post-query supersedes sweep (Milvus lookup on all returned entry_ids)
    if not include_history and filtered:
        entry_ids = [r.entry_id for r in filtered if r.entry_id]
        if entry_ids:
            superseded = await _lookup_superseded(collection, entry_ids)
            if superseded:
                filtered = [r for r in filtered if r.entry_id not in superseded]

    # Batch-fetch provenance for the final result set. Entries ingested
    # without a provenance dict won't have a row; the map lacks those
    # entry_ids and the per-result lookup yields None.
    prov_map: dict[str, dict[str, Any]] = {}
    if filtered:
        final_ids = [r.entry_id for r in filtered if r.entry_id]
        if final_ids:
            try:
                async with async_session() as session:
                    prov_map = await get_provenance_batch(session, final_ids)
            except Exception as e:
                # Failed lookup → empty map → results carry provenance=None.
                # No warnings.append: a missing provenance row is the same
                # API shape whether it's "row absent" or "DB unreachable".
                logger.warning("provenance_fetch_failed: %s", e)

    # §17.120 — quality-signal-weighted rerank. Apply a per-result
    # multiplicative bump based on quality_signal from provenance; re-sort
    # by the bumped final_score. Bumps cap at ×1.20; embedding similarity
    # remains the primary signal. Entries with no provenance row get 1.0.
    #
    # §17.197 — use ``dataclasses.replace`` to build the bumped result
    # objects instead of mutating final_score in place. The earlier
    # ``_rerank`` and ``_rrf_fuse`` paths already use replace() to keep
    # RagResult immutable from a caller's perspective; the in-place bump
    # broke that invariant. Currently practically safe (filtered doesn't
    # escape the function), but the moment a future change caches the
    # RagResult list rather than the response dict, the bump would
    # double-apply on a cache hit. Locking the no-mutation contract now.
    quality_bumps: dict[str, float] = {}
    if filtered:
        from app.modules.quality_rerank import quality_bump
        for r in filtered:
            prov = prov_map.get(r.entry_id)
            qs = (prov or {}).get("quality_signal")
            quality_bumps[r.entry_id] = quality_bump(r.source_type, qs)
        filtered = [
            replace(r, final_score=r.final_score * quality_bumps[r.entry_id])
            for r in filtered
        ]
        filtered.sort(key=lambda r: r.final_score, reverse=True)

    latency_ms = round((time.monotonic() - t0) * 1000, 1)
    top_score = round(filtered[0].final_score, 4) if filtered else 0.0
    logger.info(
        "retrieval_completed: query='%s' domain=%s n_results=%d top_score=%.4f latency_ms=%.1f",
        query[:200], domain if domain is not None else "all", len(filtered), top_score, latency_ms,
    )

    result_dicts = []
    for r in filtered:
        result_dicts.append({
            "content": r.content,
            "title": r.title,
            "tags": r.tags,
            "source_url": r.source_url,
            "entry_id": r.entry_id,
            "domain": r.domain,
            "version": r.version,
            "supersedes_id": r.supersedes_id,
            "confidence_score": r.confidence_score,
            "source_type": r.source_type,
            "provenance": prov_map.get(r.entry_id),
            "scores": {
                "vector": round(r.vector_score, 4),
                "keyword": round(r.keyword_score, 4),
                "rrf": round(r.rrf_score, 4),
                "rerank": round(r.rerank_score, 4),
                "final": round(r.final_score, 4),
                "quality_bump": round(quality_bumps.get(r.entry_id, 1.0), 4),
            },
        })

    response = {
        "status": "ok",
        "query": query,
        "result_count": len(result_dicts),
        "results": result_dicts,
        "metadata": {
            "vector_hits": len(vector_results),
            "keyword_hits": len(keyword_results),
            "fused_count": len(fused),
            "confidence_threshold": confidence_threshold,
            "threshold_relaxed": fell_back_to_top3,
            "below_threshold": below_threshold,
            "fell_back_to_top3": fell_back_to_top3,
            "reranked": not (skip_rerank or skipped_rerank),
            "skipped_rerank": skipped_rerank or skip_rerank,
            "reranker_backend": backend,
            "warnings": warnings,
            # §17.767 — partitions that RAISED during search (always present; []
            # on success). `degraded` = a suspect empty result: a failure
            # coincided with zero results, so 0 may be a failure, not real absence.
            "partitions_failed": partitions_failed,
            "degraded": bool(partitions_failed) and len(result_dicts) == 0,
            "latency_ms": latency_ms,
            # §17.253 — surface the EFFECTIVE reranker knobs used for
            # this call so an operator passing a per-request override
            # (§17.234 max_candidates, §17.252 doc_truncate) can
            # confirm it was applied. None override → settings default;
            # explicit value → that value. Both shown as the resolved
            # int even when None was passed, so the operator always
            # sees the actual numbers the reranker ran with.
            "rerank_max_candidates": int(
                max_candidates if max_candidates is not None
                else settings.rerank_max_candidates
            ),
            "rerank_doc_truncate": int(
                doc_truncate if doc_truncate is not None
                else settings.rerank_doc_truncate
            ),
        },
    }
    await rag_cache.put(
        query, domain, top_k, confidence_threshold,
        skip_rerank, include_history, query_intent,
        response,
        max_candidates=max_candidates,
        doc_truncate=doc_truncate,
        domain_hint=domain_hint,  # §17.604 — part of the cache key
    )
    return response


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

def _build_embedding_text(entry: dict) -> str:
    parts = []
    if entry.get("title"):
        parts.append(entry["title"])
    tags = entry.get("domain_tags", [])
    if tags:
        parts.append(f"Topics: {', '.join(tags)}")
    if entry.get("canonical_text"):
        parts.append(entry["canonical_text"])
    return "\n".join(parts)


def _content_hash(text_: str) -> str:
    return hashlib.sha256(normalize_cache_text(text_).encode()).hexdigest()


def _advisory_lock_key(predecessor_eid: str) -> int:
    """§17.269 — derive a stable 63-bit signed bigint from predecessor_eid.

    Used as the key for `pg_advisory_xact_lock`. Two ingests targeting the
    same predecessor compute the same key → serialize. Different
    predecessors compute different keys → no contention.
    """
    h = hashlib.sha256(predecessor_eid.encode("utf-8")).digest()[:8]
    return int.from_bytes(h, "big", signed=True)


@asynccontextmanager
async def _predecessor_lock(predecessor_eid: str):
    """§17.269 — Postgres advisory lock keyed on a predecessor entry_id.

    Yields the live AsyncSession. Lock is acquired via
    `pg_advisory_xact_lock(key)` and held until the transaction commits
    at `__aexit__`. Two concurrent ingests in the version-chain band
    (cosine 0.90-0.95) targeting the same matched_id serialize through
    this lock; different matched_ids do not contend.

    The lock window MUST span: walk-forward → upsert → commit. The
    re-walk inside the lock sees any prior holder's just-committed
    successor row, so the next ingest links to the new tail (linear
    chain) instead of branching from the stale predecessor.

    Closes the §17.267 race documented in
    `tests/test_dedup_rejection.py::test_concurrent_ingest_branches_version_chain`.
    """
    key = _advisory_lock_key(predecessor_eid)
    async with async_session() as db:
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:k)"),
            {"k": key},
        )
        try:
            yield db
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def _walk_to_latest_version(
    collection: "MilvusClient",
    entry_id: str,
    version: int,
    safe_domain: str,
) -> tuple[str, int]:
    """Walk the supersedes chain forward. Cycle-capped at _VERSION_CHAIN_WALK_MAX."""
    current_eid = entry_id
    current_version = version
    loop = asyncio.get_running_loop()

    for _ in range(_VERSION_CHAIN_WALK_MAX):
        safe_eid = _escape_literal(current_eid)

        def _sync(eid=safe_eid) -> list[dict]:
            try:
                return collection.query(
                    collection_name=COLLECTION_NAME,
                    filter=f'supersedes_id == "{eid}" and domain == "{safe_domain}"',
                    output_fields=["entry_id", "version"],
                    limit=1,
                )
            except Exception as e:
                logger.debug("version_walk_query_failed: %s", e)
                return []

        rows = await loop.run_in_executor(None, _sync)
        if not rows:
            return current_eid, current_version
        newer = rows[0]
        next_eid = newer.get("entry_id", "")
        next_version = int(newer.get("version", current_version + 1))
        if not next_eid or next_eid == current_eid:
            break
        current_eid = next_eid
        current_version = next_version

    return current_eid, current_version


async def ingest_entries(
    entries: list[dict], domain: str = "eng",
    *, session_id: str | None = None,
    progress_cb=None,
) -> "IngestStatsDict":
    """Embed and upsert knowledge entries into toon_v2.

    Returns: {new, versioned, rejected, skipped_hash, skipped_empty}.
    Upsert is keyed on entry_id, closing the hash-check+insert race where
    two concurrent ingests of the same logical entry would duplicate.

    §17.811 — ``progress_cb(completed, total)`` is an optional synchronous hook
    invoked (throttled) as Pass 3 upserts each prepared entry, so a caller can
    surface ingest progress. ``total`` is the count entering Pass 3 (post
    exact-hash filter). Fail-soft: a raising callback is swallowed, never
    breaking ingest.
    """
    stats = {"new": 0, "versioned": 0, "rejected": 0, "skipped_hash": 0, "skipped_empty": 0}
    if not entries:
        return stats
    if domain == "":
        raise ValueError('domain="" is not allowed for ingest')
    # §17.769 (Phase 3 backstop) — a domain outside VALID_DOMAINS lands data in a
    # partition-key value no all-partition search ever queries (silently stranded).
    # The schema validators reject it at the API; coerce+warn here too so any path
    # that bypasses the schema can't strand data. "eng" is the safe default member.
    from app.config import VALID_DOMAINS
    if domain not in VALID_DOMAINS:
        logger.warning(
            "ingest_entries: unknown domain %r not in VALID_DOMAINS %s — coercing "
            "to 'eng' to avoid stranding data", domain, sorted(VALID_DOMAINS),
        )
        domain = "eng"

    loop = asyncio.get_running_loop()
    collection = await loop.run_in_executor(None, _get_client)
    if collection is None:
        logger.error("ingest_entries: collection not available")
        return stats

    now = int(time.time())
    safe_domain = _escape_literal(domain)
    provenance_writes: list[tuple[str, dict, str | None]] = []
    # §17.172 — dedup_log writes deferred + batched. Pre-§17.172 the
    # 'versioned' branch wrote its INSERT in its own session *before* the
    # corresponding Milvus upsert. If the upsert failed, the dedup_log
    # carried a 'versioned' row whose successor never materialized,
    # breaking the audit invariant "if dedup_log says 'versioned',
    # the version chain exists in Milvus." Now: stash all writes here,
    # append the 'versioned' tuple only AFTER the Milvus upsert succeeds,
    # commit once at the end. The 'rejected' path (no follow-up upsert)
    # also flows through this list — equivalent behavior, single commit.
    # Tuple shape: (new_content_hash, existing_entry_id, similarity_score, action).
    dedup_log_writes: list[tuple[str, str, float, str]] = []

    # ---- Pass 1: normalize + exact-hash filter ----
    # §17.616 (audit #31) — all entries in a call share safe_domain, so the
    # exact-hash dedup collapses to ONE `content_hash in [...] and domain == D`
    # query instead of one sequential Milvus round-trip per entry.
    candidates: list[dict] = []
    for entry in entries:
        norm = _normalize_entry(entry)
        content = norm["content"]
        if not content:
            stats["skipped_empty"] += 1
            continue

        title = norm["title"]
        domain_tags = norm["domain_tags"]
        source_url = norm["source_url"]
        source_type = norm["source_type"]
        # _normalize_entry defaults confidence to 0.60, which would mask
        # "caller didn't supply one" — check the raw entry to decide
        # whether to derive from source_type instead.
        explicit_confidence = entry.get("confidence", entry.get("confidence_score"))
        confidence = confidence_for(source_type, explicit_confidence)
        provenance = entry.get("provenance")
        raw_upstream_hash = entry.get("raw_upstream_hash")
        ch = _content_hash(content)

        embed_text = _build_embedding_text({
            "title": title,
            "domain_tags": domain_tags,
            "canonical_text": content,
        })
        candidates.append({
            "title": title, "content": content, "domain_tags": domain_tags,
            "source_url": source_url, "source_type": source_type,
            "confidence": confidence, "ch": ch, "embed_text": embed_text,
            "provenance": provenance,
            "raw_upstream_hash": raw_upstream_hash,
        })

    # One batched exact-hash lookup for the whole call.
    present_hashes: set[str] = set()
    if candidates:
        hashes = list({c["ch"] for c in candidates})
        in_list = ", ".join(f'"{h}"' for h in hashes)
        try:
            existing = await loop.run_in_executor(
                None,
                lambda: collection.query(
                    collection_name=COLLECTION_NAME,
                    filter=f'content_hash in [{in_list}] and domain == "{safe_domain}"',
                    output_fields=["content_hash"],
                    limit=len(hashes),
                ),
            )
            present_hashes = {r.get("content_hash") for r in (existing or [])}
        except Exception as e:
            logger.debug("dedup_check_failed: %s", e)

    prepared: list[dict] = []
    for c in candidates:
        if c["ch"] in present_hashes:
            logger.debug("dedup_skip: exact hash match for '%s'", c["title"][:50])
            stats["skipped_hash"] += 1
            continue
        prepared.append(c)

    if not prepared:
        return stats

    # ---- Pass 2: batch embed ----
    vectors = await _embed_contents_batch([p["embed_text"] for p in prepared])

    dedup_threshold = float(settings.semantic_dedup_threshold)
    version_threshold = float(settings.version_chain_threshold)

    # ---- Pass 3: semantic dedup + version chain + upsert ----
    # §17.811 — throttle progress callbacks so a large batch doesn't spam the
    # caller; the final entry always reports (handled after the loop).
    from app.utils.progress import EmitThrottle as _EmitThrottle

    _ing_total = len(prepared)
    _ing_thr = _EmitThrottle(settings.progress_emit_min_interval_seconds)

    def _emit_ingest_progress(done: int, *, final: bool = False) -> None:
        if progress_cb is None:
            return
        if _ing_thr.ready(final=final):
            try:
                progress_cb(done, _ing_total)
            except Exception:  # noqa: BLE001 — progress is best-effort
                pass

    for _idx, (p, vector) in enumerate(zip(prepared, vectors)):
        _emit_ingest_progress(_idx)
        if vector is None:
            logger.warning("ingest_embed_failed for title=%s", p["title"])
            continue

        new_version = 1
        new_supersedes = ""
        # §17.172 — captured in the supersede branch below + read in the
        # post-upsert dedup_log append. 0.0 sentinel = "no supersede happened
        # for this entry"; only consulted when new_supersedes is non-empty.
        version_sim_score: float = 0.0

        try:
            sim_results = await loop.run_in_executor(
                None,
                lambda v=vector: collection.search(
                    collection_name=COLLECTION_NAME,
                    data=[v],
                    anns_field="dense_vector",
                    search_params={"metric_type": "COSINE", "params": {"ef": 32}},
                    limit=5,
                    filter=f'domain == "{safe_domain}"',
                    output_fields=["entry_id", "content_hash", "version", "supersedes_id"],
                ),
            )
            if sim_results and sim_results[0]:
                top_hit = sim_results[0][0]
                sim_score = float(top_hit["distance"])

                if sim_score >= dedup_threshold:
                    # The Pass 1 exact-hash filter (search above for
                    # "Pass 1: normalize + exact-hash filter") catches identity
                    # matches in serial flow. Concurrent ingests can race past
                    # it (Entry A is mid-pipeline with hash H; Entry B reaches
                    # Pass 1 before A is upserted, also passes Pass 1; Pass 3
                    # sees A already in Milvus). Reject by similarity
                    # unconditionally here so the racing duplicate doesn't slip
                    # into the version-chain branch. §17.265 — replaced the
                    # pre-§17.265 "L738-750" line reference; line numbers rot.
                    existing_eid = top_hit["entity"].get("entry_id", str(top_hit["id"]))
                    logger.info(
                        "dedup_rejected: sim=%.4f title='%s' existing='%s'",
                        sim_score, p["title"][:50], existing_eid,
                    )
                    # §17.172 — rejected rows have no Milvus follow-up, so
                    # append unconditionally. The batched commit at the end
                    # of ingest_entries persists this alongside all other
                    # dedup_log writes.
                    dedup_log_writes.append((p["ch"], existing_eid, sim_score, "rejected"))
                    stats["rejected"] += 1
                    continue
                elif sim_score >= version_threshold:
                    # §17.269 — version-chain entries do walk + upsert
                    # inside the predecessor lock so two concurrent ingests
                    # targeting the same matched_id serialize. The re-walk
                    # inside the lock sees the prior holder's commit, so
                    # the chain stays LINEAR instead of branching.
                    # `continue` at the end of the lock block skips the
                    # common upsert path below; new-entry path falls through.
                    candidate_eid = top_hit["entity"].get("entry_id", str(top_hit["id"]))
                    candidate_version = int(top_hit["entity"].get("version", 1))
                    version_sim_score = sim_score
                    async with _predecessor_lock(candidate_eid):
                        # Authoritative walk happens HERE, inside the lock.
                        # Pre-§17.269 the walk was lockless and the result
                        # raced with concurrent upserts (see §17.267 docs).
                        latest_eid, latest_version = await _walk_to_latest_version(
                            collection, candidate_eid, candidate_version, safe_domain
                        )
                        new_version = latest_version + 1
                        new_supersedes = latest_eid
                        logger.info(
                            "version_chain_linked: v%d supersedes='%s' sim=%.4f title='%s'",
                            new_version, latest_eid, sim_score, p["title"][:50],
                        )
                        _slug = re.sub(r"[^a-z0-9]+", "-", p["title"].lower()).strip("-")[:60]
                        topic_slug = _slug or "untitled"
                        entry_id = f"scaffold-{topic_slug}-{p['ch'][:8]}"
                        row = [{
                            "entry_id": entry_id,
                            "title": p["title"],
                            "canonical_text": p["content"],
                            "domain": domain,
                            "domain_tags": p["domain_tags"],
                            "confidence_score": float(p["confidence"]),
                            "source_type": p["source_type"],
                            "source_url": p["source_url"],
                            "content_hash": p["ch"],
                            "model_id": settings.model_embedder_id,
                            "version": new_version,
                            "supersedes_id": new_supersedes,
                            "created_at": now,
                            "updated_at": now,
                            "expires_at": compute_expires_at(p["source_type"], now),
                            "dense_vector": vector,
                        }]
                        try:
                            await loop.run_in_executor(
                                None, lambda r=row: collection.upsert(collection_name=COLLECTION_NAME, data=r)
                            )
                            stats["versioned"] += 1
                            # §17.172 — dedup_log 'versioned' append happens
                            # AFTER the upsert succeeds (gated). Tuple shape
                            # mirrors the rejection branch above.
                            dedup_log_writes.append(
                                (p["ch"], new_supersedes, version_sim_score, "versioned")
                            )
                            if p["provenance"] or p["raw_upstream_hash"]:
                                provenance_writes.append(
                                    (entry_id, p["provenance"] or {}, p["raw_upstream_hash"])
                                )
                        except Exception as e:
                            logger.warning("ingest_upsert_failed: %s", e)
                    # Lock released; row recorded. Skip the new-entry path.
                    continue
        except Exception as e:
            logger.debug("semantic_dedup_failed: %s", e)

        # §17.269 — only NEW entries (no predecessor) reach here. Version-
        # chain entries took the lock + upsert path above and `continue`d.
        _slug = re.sub(r"[^a-z0-9]+", "-", p["title"].lower()).strip("-")[:60]
        topic_slug = _slug or "untitled"
        entry_id = f"scaffold-{topic_slug}-{p['ch'][:8]}"

        row = [{
            "entry_id": entry_id,
            "title": p["title"],
            "canonical_text": p["content"],
            "domain": domain,
            "domain_tags": p["domain_tags"],
            "confidence_score": float(p["confidence"]),
            "source_type": p["source_type"],
            "source_url": p["source_url"],
            "content_hash": p["ch"],
            "model_id": settings.model_embedder_id,
            "version": new_version,
            "supersedes_id": new_supersedes,
            "created_at": now,
            "updated_at": now,
            "expires_at": compute_expires_at(p["source_type"], now),
            "dense_vector": vector,
        }]

        try:
            await loop.run_in_executor(
                None, lambda r=row: collection.upsert(collection_name=COLLECTION_NAME, data=r)
            )
            stats["new"] += 1
            if p["provenance"] or p["raw_upstream_hash"]:
                provenance_writes.append((entry_id, p["provenance"] or {}, p["raw_upstream_hash"]))
        except Exception as e:
            logger.warning("ingest_upsert_failed: %s", e)

    _emit_ingest_progress(_ing_total, final=True)  # §17.811 — terminal 100%

    inserted = stats["new"] + stats["versioned"]
    if inserted > 0:
        await loop.run_in_executor(None, lambda: collection.flush(collection_name=COLLECTION_NAME))
        logger.info(
            "ingested %d (new=%d versioned=%d rejected=%d hash_skipped=%d) into toon_v2",
            inserted, stats["new"], stats["versioned"], stats["rejected"], stats["skipped_hash"],
        )

    if provenance_writes:
        try:
            async with async_session() as session:
                # §17.616 (audit #33) — one multi-row INSERT for the whole batch
                # instead of N single-row round-trips.
                await write_provenance_batch(
                    session, provenance_writes, session_id=session_id,
                )
                await session.commit()
        except Exception as e:
            logger.error("provenance_batch_write_failed: %s n=%d", e, len(provenance_writes))

    # §17.172 — batched dedup_log commit. Separate session from the
    # provenance batch above because the failure modes are distinct:
    # dedup_log is the supersede ledger (invariant #9), provenance is
    # the source-URL audit pointer. A failure in one must not roll back
    # the other. Single session inside this block for write-amplification
    # (one round-trip for all rows of either action_taken).
    if dedup_log_writes:
        try:
            async with async_session() as session:
                for ch_hash, eid, score, action in dedup_log_writes:
                    await session.execute(
                        text(
                            "INSERT INTO dedup_log (new_content_hash, existing_entry_id, similarity_score, action_taken) "
                            "VALUES (:hash, :eid, :score, :action)"
                        ),
                        {"hash": ch_hash, "eid": eid, "score": score, "action": action},
                    )
                await session.commit()
        except Exception as e:
            logger.error("dedup_log_batch_write_failed: %s n=%d", e, len(dedup_log_writes))

    return stats
