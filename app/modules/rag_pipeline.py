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
from dataclasses import dataclass, field, replace
from typing import Any

from pymilvus import Collection
from app.utils.milvus_utils import get_collection
from app.rerankers import rerank as cross_encoder_rerank

from app import model_router
from app.config import settings, VALID_DOMAINS
from app.utils.staleness import compute_expires_at
from app.utils.embedding_cache import get_cache, truncate_and_normalize, normalize_cache_text

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


_get_collection = get_collection


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


def _iter_search_domains(domain: str | None) -> list[str]:
    """Expand a user-supplied domain arg into the list of domains to fan out to.

    Milvus partition-key isolation rejects both "no expr" and "IN" exprs, so a
    caller asking for domain=None is served by running one == search per
    configured partition and merging the results.
    """
    if domain is None:
        return sorted(VALID_DOMAINS)
    if domain == "":
        raise ValueError('domain="" is not allowed; pass None to search all partitions')
    return [domain]


# ---------------------------------------------------------------------------
# Embedding helpers
# ---------------------------------------------------------------------------

async def _embed_query(query: str) -> list[float] | None:
    """Embed query — delegates to app.utils.embedding.embed_query."""
    from app.utils.embedding import embed_query as _public_embed_query
    return await _public_embed_query(query)


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
    collection: Collection,
    query_embedding: list[float],
    top_k: int,
    domain: str | None = None,
) -> list[RagResult]:
    """ANN search in Milvus (off event loop).

    Fans out one search per partition when domain is None. Under partition-key
    isolation Milvus rejects both "no expr" and "IN" over the partition key,
    so per-partition == exprs are the only safe path.
    """
    domains = _iter_search_domains(domain)

    def _sync() -> list[RagResult]:
        if collection is None:
            return []
        all_hits: list[RagResult] = []
        for d in domains:
            try:
                search_kwargs: dict[str, Any] = dict(
                    data=[query_embedding],
                    anns_field="dense_vector",
                    param={"metric_type": "COSINE", "params": {"ef": 128, "refine_k": 2}},
                    limit=top_k,
                    expr=f'domain == "{_escape_literal(d)}"',
                    output_fields=[
                        "canonical_text", "title", "domain_tags", "source_url",
                        "entry_id", "domain", "confidence_score", "version",
                        "supersedes_id",
                    ],
                )
                results = collection.search(**search_kwargs)
                for hit in results[0]:
                    entity = hit.entity
                    tags_list = entity.get("domain_tags", [])
                    tags_str = ", ".join(tags_list) if isinstance(tags_list, list) else str(tags_list)
                    all_hits.append(RagResult(
                        content=entity.get("canonical_text", ""),
                        title=entity.get("title", ""),
                        tags=tags_str,
                        source_url=entity.get("source_url", ""),
                        entry_id=entity.get("entry_id", ""),
                        domain=entity.get("domain", ""),
                        vector_score=float(hit.score),
                        version=entity.get("version", 1),
                        supersedes_id=entity.get("supersedes_id", ""),
                    ))
            except Exception as e:
                logger.warning("Vector search failed (domain=%s): %s", d, e)
                continue
        # Merge across partitions: sort by score desc, keep top_k
        all_hits.sort(key=lambda r: r.vector_score, reverse=True)
        return all_hits[:top_k]

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync)


# ---------------------------------------------------------------------------
# Keyword search
# ---------------------------------------------------------------------------

async def _keyword_search(
    collection: Collection,
    query: str,
    top_k: int,
    domain: str | None = None,
) -> list[RagResult]:
    """Keyword-based search (off event loop).

    Tokens are restricted to [a-z0-9]+ via _KEYWORD_TERM_RE — eliminates
    LIKE wildcards, escape chars, and quotes from the interpolation path.

    Fans out one query per partition when domain is None (partition-key
    isolation rejects IN and unfiltered exprs).
    """
    tokens = _KEYWORD_TERM_RE.findall(query.lower())
    words = [t for t in tokens if len(t) >= 3 and t not in _STOPWORDS]
    if not words:
        return []

    conditions: list[str] = []
    for word in words[:KEYWORD_MAX_TERMS]:
        conditions.append(f'canonical_text like "%{word}%"')
        conditions.append(f'title like "%{word}%"')
    keyword_expr = " or ".join(conditions)

    domains = _iter_search_domains(domain)

    def _sync() -> list[RagResult]:
        if collection is None:
            return []
        all_hits: list[RagResult] = []
        for d in domains:
            expr = f'domain == "{_escape_literal(d)}" and ({keyword_expr})'
            try:
                results = collection.query(
                    expr=expr,
                    output_fields=[
                        "canonical_text", "title", "domain_tags", "source_url",
                        "entry_id", "domain", "version", "supersedes_id",
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
                    all_hits.append(RagResult(
                        content=r.get("canonical_text", ""),
                        title=r.get("title", ""),
                        tags=tags_str,
                        source_url=r.get("source_url", ""),
                        entry_id=r.get("entry_id", ""),
                        domain=r.get("domain", ""),
                        keyword_score=score,
                        version=r.get("version", 1),
                        supersedes_id=r.get("supersedes_id", ""),
                    ))
            except Exception as e:
                logger.warning("Keyword search failed (domain=%s): %s", d, e)
                continue
        all_hits.sort(key=lambda r: r.keyword_score, reverse=True)
        return all_hits[:top_k]

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync)


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
) -> tuple[list[RagResult], dict[str, Any]]:
    """Rerank via CrossEncoder. Returns (ranked, meta).

    meta contains: backend, skipped_rerank, warnings (list[str]).
    """
    meta: dict[str, Any] = {"backend": None, "skipped_rerank": False, "warnings": []}

    if not results:
        return [], meta

    max_cand = int(settings.rerank_max_candidates)
    doc_trunc = int(settings.rerank_doc_truncate)
    warn_ms = int(settings.rerank_warn_ms)
    error_ms = int(settings.rerank_error_ms)

    docs = [r.content[:doc_trunc] for r in results[:max_cand]]

    loop = asyncio.get_running_loop()
    rr = await loop.run_in_executor(None, cross_encoder_rerank, query, docs, len(docs))

    meta["backend"] = getattr(rr, "backend", None)

    # Empty items but non-empty docs = reranker silently produced nothing.
    # Surface as an explicit WARNING and fall back to RRF ordering.
    # Uses dataclasses.replace to honor _rrf_fuse's no-mutation contract —
    # callers may hold references to results that pre-date this rerank.
    if not rr.items and docs:
        logger.warning(
            "rerank_skipped: backend=%s returned 0 items for %d docs; falling back to RRF order",
            meta["backend"], len(docs),
        )
        meta["skipped_rerank"] = True
        meta["warnings"].append("reranker_returned_no_items")
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
        ),
    )

    results.sort(key=lambda r: r.final_score, reverse=True)
    return results[:top_k], meta


# ---------------------------------------------------------------------------
# Supersedes sweep
# ---------------------------------------------------------------------------

async def _lookup_superseded(
    collection: Collection, entry_ids: list[str]
) -> set[str]:
    """Return the subset of entry_ids that are superseded by some other row.

    Queries: supersedes_id IN (entry_ids). A hit means some newer row points
    at one of our ids → that id is stale. Closes overview issue #7.
    """
    if not entry_ids:
        return set()

    quoted = [f'"{_escape_literal(eid)}"' for eid in entry_ids]
    expr = f"supersedes_id in [{', '.join(quoted)}]"

    def _sync() -> set[str]:
        try:
            rows = collection.query(
                expr=expr,
                output_fields=["supersedes_id"],
                limit=max(1, len(entry_ids) * 4),
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
    top_k: int = DEFAULT_TOP_K,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
    skip_rerank: bool = False,
    include_history: bool = False,
) -> dict[str, Any]:
    """Full RAG pipeline: embed → search → fuse → rerank → filter → supersede-sweep."""
    top_k = max(1, min(top_k, MAX_TOP_K))
    t0 = time.monotonic()

    warnings: list[str] = []

    loop = asyncio.get_running_loop()
    collection = await loop.run_in_executor(None, _get_collection)
    if collection is None:
        return {
            "status": "error",
            "error": f"Collection '{COLLECTION_NAME}' not available",
            "results": [],
            "metadata": {"warnings": ["collection_unavailable"], "reranker_backend": None},
        }

    query_embedding = await _embed_query(query)
    if query_embedding is None:
        return {
            "status": "error",
            "error": "Failed to generate query embedding",
            "results": [],
            "metadata": {"warnings": ["embed_failed"], "reranker_backend": None},
        }

    vector_results, keyword_results = await asyncio.gather(
        _vector_search(collection, query_embedding, top_k * 2, domain=domain),
        _keyword_search(collection, query, top_k * 2, domain=domain),
    )

    logger.info(
        "search_executed: vector_hits=%d keyword_hits=%d query='%s'",
        len(vector_results), len(keyword_results), query[:50],
    )

    fused = _rrf_fuse(vector_results, keyword_results)

    rerank_meta: dict[str, Any] = {"skipped_rerank": False, "backend": None, "warnings": []}
    if skip_rerank or not fused:
        for r in fused:
            r.final_score = r.rrf_score
        ranked = fused[:top_k]
        if skip_rerank:
            rerank_meta["skipped_rerank"] = True
    else:
        ranked, rerank_meta = await _rerank(query, fused, top_k)

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
            "scores": {
                "vector": round(r.vector_score, 4),
                "keyword": round(r.keyword_score, 4),
                "rrf": round(r.rrf_score, 4),
                "rerank": round(r.rerank_score, 4),
                "final": round(r.final_score, 4),
            },
        })

    return {
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
            "latency_ms": latency_ms,
        },
    }


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


async def _walk_to_latest_version(
    collection: Collection,
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
                    expr=f'supersedes_id == "{eid}" and domain == "{safe_domain}"',
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


async def ingest_entries(entries: list[dict], domain: str = "eng") -> dict:
    """Embed and upsert knowledge entries into toon_v2.

    Returns: {new, versioned, rejected, skipped_hash, skipped_empty}.
    Upsert is keyed on entry_id, closing the hash-check+insert race where
    two concurrent ingests of the same logical entry would duplicate.
    """
    stats = {"new": 0, "versioned": 0, "rejected": 0, "skipped_hash": 0, "skipped_empty": 0}
    if not entries:
        return stats
    if domain == "":
        raise ValueError('domain="" is not allowed for ingest')

    loop = asyncio.get_running_loop()
    collection = await loop.run_in_executor(None, _get_collection)
    if collection is None:
        logger.error("ingest_entries: collection not available")
        return stats

    now = int(time.time())
    safe_domain = _escape_literal(domain)

    # ---- Pass 1: normalize + exact-hash filter ----
    prepared: list[dict] = []
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
        confidence = norm["confidence"]
        ch = _content_hash(content)

        try:
            existing = await loop.run_in_executor(
                None,
                lambda h=ch: collection.query(
                    expr=f'content_hash == "{h}" and domain == "{safe_domain}"',
                    output_fields=["entry_id"],
                    limit=1,
                ),
            )
            if existing:
                logger.debug("dedup_skip: exact hash match for '%s'", title[:50])
                stats["skipped_hash"] += 1
                continue
        except Exception as e:
            logger.debug("dedup_check_failed: %s", e)

        embed_text = _build_embedding_text({
            "title": title,
            "domain_tags": domain_tags,
            "canonical_text": content,
        })
        prepared.append({
            "title": title, "content": content, "domain_tags": domain_tags,
            "source_url": source_url, "source_type": source_type,
            "confidence": confidence, "ch": ch, "embed_text": embed_text,
        })

    if not prepared:
        return stats

    # ---- Pass 2: batch embed ----
    vectors = await _embed_contents_batch([p["embed_text"] for p in prepared])

    dedup_threshold = float(settings.semantic_dedup_threshold)
    version_threshold = float(settings.version_chain_threshold)

    # ---- Pass 3: semantic dedup + version chain + upsert ----
    for p, vector in zip(prepared, vectors):
        if vector is None:
            logger.warning("ingest_embed_failed for title=%s", p["title"])
            continue

        new_version = 1
        new_supersedes = ""

        try:
            sim_results = await loop.run_in_executor(
                None,
                lambda v=vector: collection.search(
                    data=[v],
                    anns_field="dense_vector",
                    param={"metric_type": "COSINE", "params": {"ef": 32}},
                    limit=5,
                    expr=f'domain == "{safe_domain}"',
                    output_fields=["entry_id", "content_hash", "version", "supersedes_id"],
                ),
            )
            if sim_results and sim_results[0]:
                top_hit = sim_results[0][0]
                sim_score = float(top_hit.score)

                if sim_score >= dedup_threshold:
                    # The exact-hash filter at L738-750 catches identity matches
                    # in serial flow. Concurrent ingests can race past it (Entry
                    # A is mid-pipeline with hash H; Entry B reaches Pass 1
                    # before A is upserted, also passes Pass 1; Pass 3 sees A
                    # already in Milvus). Reject by similarity unconditionally
                    # here so the racing duplicate doesn't slip into the
                    # version-chain branch.
                    existing_eid = top_hit.entity.get("entry_id", str(top_hit.id))
                    logger.info(
                        "dedup_rejected: sim=%.4f title='%s' existing='%s'",
                        sim_score, p["title"][:50], existing_eid,
                    )
                    try:
                        async with async_session() as session:
                            await session.execute(
                                text(
                                    "INSERT INTO dedup_log (new_content_hash, existing_entry_id, similarity_score, action_taken) "
                                    "VALUES (:hash, :eid, :score, 'rejected')"
                                ),
                                {"hash": p["ch"], "eid": existing_eid, "score": sim_score},
                            )
                            await session.commit()
                    except Exception as db_err:
                        logger.error("dedup_log_write_failed: %s", db_err)
                    stats["rejected"] += 1
                    continue
                elif sim_score >= version_threshold:
                    # Walk forward to latest version to avoid mid-chain pointers.
                    candidate_eid = top_hit.entity.get("entry_id", str(top_hit.id))
                    candidate_version = int(top_hit.entity.get("version", 1))
                    latest_eid, latest_version = await _walk_to_latest_version(
                        collection, candidate_eid, candidate_version, safe_domain
                    )
                    new_version = latest_version + 1
                    new_supersedes = latest_eid
                    logger.info(
                        "version_chain_linked: v%d supersedes='%s' sim=%.4f title='%s'",
                        new_version, latest_eid, sim_score, p["title"][:50],
                    )
                    # Audit: superseded entries get a dedup_log row alongside
                    # rejections (invariant #9). Action='versioned' to
                    # distinguish from outright rejection.
                    try:
                        async with async_session() as session:
                            await session.execute(
                                text(
                                    "INSERT INTO dedup_log (new_content_hash, existing_entry_id, similarity_score, action_taken) "
                                    "VALUES (:hash, :eid, :score, 'versioned')"
                                ),
                                {"hash": p["ch"], "eid": latest_eid, "score": sim_score},
                            )
                            await session.commit()
                    except Exception as db_err:
                        logger.error("dedup_log_write_failed: %s", db_err)
        except Exception as e:
            logger.debug("semantic_dedup_failed: %s", e)

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
                None, lambda r=row: collection.upsert(r)
            )
            if new_supersedes:
                stats["versioned"] += 1
            else:
                stats["new"] += 1
        except Exception as e:
            logger.warning("ingest_upsert_failed: %s", e)

    inserted = stats["new"] + stats["versioned"]
    if inserted > 0:
        await loop.run_in_executor(None, collection.flush)
        logger.info(
            "ingested %d (new=%d versioned=%d rejected=%d hash_skipped=%d) into toon_v2",
            inserted, stats["new"], stats["versioned"], stats["rejected"], stats["skipped_hash"],
        )

    return stats
