"""Scaffold Engine -- RAG pipeline module.

Query flow:
  1. Embed query (qwen3-embedding:8b, MRL truncated to 512d)
  2. Milvus ANN search (HNSW_SQ8 COSINE on toon_v2)
  3. Keyword search (canonical_text/title filter)
  4. RRF fusion of both result sets
  5. Cross-encoder rerank (Qwen3-Reranker-0.6B via sentence-transformers)
  6. Confidence filter + dynamic top-k
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from pymilvus import Collection
from app.utils.milvus_utils import get_collection
from app.rerankers import rerank as cross_encoder_rerank

from app import model_router
from app.config import settings
from app.utils.staleness import compute_expires_at
from app.utils.embedding_cache import get_cache, truncate_and_normalize

from sqlalchemy import text
from app.database import async_session

logger = logging.getLogger("scaffold.rag")

COLLECTION_NAME = "toon_v2"
EMBED_DIM = 512
DEFAULT_TOP_K = 10
CONFIDENCE_THRESHOLD = 0.8
RRF_K = 60  # RRF smoothing constant


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------

@dataclass
class RagResult:
    """Single retrieval result."""
    content: str = ""
    title: str = ""
    topic: str = ""
    tags: str = ""
    source_file: str = ""
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


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Milvus collection (delegates to shared utility)
# ---------------------------------------------------------------------------
_get_collection = get_collection
# Embedding helper
# ---------------------------------------------------------------------------

async def _embed_query(query: str) -> list[float] | None:
    """Embed query (thin wrapper — delegates to app.utils.embedding.embed_query).

    Kept for backward compatibility with internal rag_pipeline callers.
    External modules should import ``embed_query`` from app.utils.embedding.
    """
    from app.utils.embedding import embed_query as _public_embed_query
    return await _public_embed_query(query)


async def _embed_content(content: str) -> list[float] | None:
    """Embed content text (no instruction prefix), MRL truncation, and cache."""
    cache = get_cache()
    cached = await cache.get(content)
    if cached:
        return cached

    embeddings = await model_router.embed(content, model=settings.model_embedder_pipeline)
    if not embeddings or not embeddings[0]:
        return None

    truncated = truncate_and_normalize(embeddings[0])
    await cache.put(content, truncated)
    return truncated


# ---------------------------------------------------------------------------
# Vector search
# ---------------------------------------------------------------------------

async def _vector_search(
    collection: Collection,
    query_embedding: list[float],
    top_k: int,
    domain: str | None = None,
) -> list[RagResult]:
    """ANN search in Milvus using query embedding (off event loop)."""
    def _sync() -> list[RagResult]:
        try:
            search_kwargs = dict(
                data=[query_embedding],
                anns_field="dense_vector",
                param={"metric_type": "COSINE", "params": {"ef": 128, "refine_k": 2}},
                limit=top_k,
                output_fields=["canonical_text", "title", "domain_tags", "source_url", "entry_id", "domain", "confidence_score", "version", "supersedes_id"],
            )
            search_domain = domain or "eng"
            search_kwargs["expr"] = f'domain == "{search_domain}"'

            col = _get_collection() if collection is None else collection
            if col is None:
                return []

            results = col.search(**search_kwargs)

            hits = []
            for hit in results[0]:
                entity = hit.entity
                tags_list = entity.get("domain_tags", [])
                tags_str = ", ".join(tags_list) if isinstance(tags_list, list) else str(tags_list)
                hits.append(RagResult(
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
            return hits
        except Exception as e:
            logger.warning("Vector search failed: %s", e)
            return []

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync)


# ---------------------------------------------------------------------------
# Keyword search (canonical_text/title filter via Milvus expressions)
# ---------------------------------------------------------------------------

async def _keyword_search(
    collection: Collection,
    query: str,
    top_k: int,
    domain: str | None = None,
) -> list[RagResult]:
    """Keyword-based search using Milvus query expressions (off event loop)."""
    stopwords = {"the", "and", "for", "are", "but", "not", "you", "all", "can", "has", "was", "one", "our", "out",
                 "how", "what", "when", "where", "which", "who", "why", "with", "this", "that", "from", "into"}
    words = [w.strip().lower() for w in query.split() if len(w.strip()) >= 3 and w.strip().lower() not in stopwords]

    if not words:
        return []

    conditions = []
    for word in words[:5]:
        safe_word = word.replace("'", "\\'").replace('"', '\\"')
        conditions.append(f'canonical_text like "%{safe_word}%"')
        conditions.append(f'title like "%{safe_word}%"')

    expr = " or ".join(conditions)
    search_domain = domain or "eng"
    expr = f'domain == "{search_domain}" and ({expr})'

    def _sync() -> list[RagResult]:
        try:
            col = _get_collection() if collection is None else collection
            if col is None:
                return []

            results = col.query(
                expr=expr,
                output_fields=["canonical_text", "title", "domain_tags", "source_url", "entry_id", "domain", "version", "supersedes_id"],
                limit=top_k,
            )

            hits = []
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
                ))
            return hits
        except Exception as e:
            logger.warning("Keyword search failed: %s", e)
            return []

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
    """Reciprocal Rank Fusion of vector and keyword result sets."""
    merged: dict[str, RagResult] = {}

    for rank, result in enumerate(vector_results):
        key = result.content[:200]
        if key not in merged:
            merged[key] = result
        merged[key].rrf_score += 1.0 / (k + rank + 1)
        merged[key].vector_score = max(merged[key].vector_score, result.vector_score)

    for rank, result in enumerate(sorted(keyword_results, key=lambda r: r.keyword_score, reverse=True)):
        key = result.content[:200]
        if key not in merged:
            merged[key] = result
        merged[key].rrf_score += 1.0 / (k + rank + 1)
        merged[key].keyword_score = max(merged[key].keyword_score, result.keyword_score)

    fused = sorted(merged.values(), key=lambda r: r.rrf_score, reverse=True)
    return fused


# ---------------------------------------------------------------------------
# Cross-encoder reranking
# ---------------------------------------------------------------------------
async def _rerank(
    query: str,
    results: list[RagResult],
    top_k: int,
) -> list[RagResult]:
    """Rerank results using CrossEncoder (sentence-transformers), RRF fallback."""
    if not results:
        return []

    docs = [r.content[:500] for r in results[:20]]

    loop = asyncio.get_running_loop()
    rr = await loop.run_in_executor(None, cross_encoder_rerank, query, docs, len(docs))

    score_map = {item.index: item.score for item in rr.items}
    for i, r in enumerate(results[:20]):
        if i in score_map:
            r.rerank_score = score_map[i]
            r.final_score = score_map[i]
        else:
            r.rerank_score = r.rrf_score
            r.final_score = r.rrf_score

    for r in results[20:]:
        r.rerank_score = r.rrf_score
        r.final_score = r.rrf_score

    scores = [item.score for item in rr.items]
    _log_reranker = logger.error if rr.latency_ms > 15000 else logger.warning if rr.latency_ms > 5000 else logger.info
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
    return results[:top_k]


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
    """Full RAG pipeline: embed -> search -> fuse -> rerank -> filter."""
    t0 = time.monotonic()

    collection = _get_collection()
    if collection is None:
        return {
            "status": "error",
            "error": f"Collection '{COLLECTION_NAME}' not available",
            "results": [],
        }

    query_embedding = await _embed_query(query)
    if query_embedding is None:
        return {
            "status": "error",
            "error": "Failed to generate query embedding",
            "results": [],
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

    if skip_rerank or not fused:
        for r in fused:
            r.final_score = r.rrf_score
        ranked = fused[:top_k]
    else:
        ranked = await _rerank(query, fused, top_k)

    # #29: confidence_threshold is only meaningful for reranker scores.
    # RRF scores top at ~0.016 and would always fail a reasonable threshold.
    # When skip_rerank=True, bypass the filter and return ranked results as-is.
    # When confidence_threshold<=0.0, also bypass (documented disable via #121).
    if skip_rerank or confidence_threshold <= 0.0:
        filtered = ranked
        too_strict = False
    else:
        filtered = [r for r in ranked if r.final_score >= confidence_threshold]
        too_strict = len(filtered) == 0 and len(ranked) > 0
        if too_strict:
            # #113: scale fallback with top_k instead of hardcoded 3.
            filtered = ranked[:min(3, top_k)]

    latency_ms = round((time.monotonic() - t0) * 1000, 1)
    top_score = round(filtered[0].final_score, 4) if filtered else 0.0
    logger.info(
        "retrieval_completed: query='%s' domain=%s n_results=%d top_score=%.4f latency_ms=%.1f",
        query[:200], domain or "all", len(filtered), top_score, latency_ms,
    )

    # Filter to latest versions unless history requested
    if not include_history:
        superseded_ids = {r.supersedes_id for r in filtered if r.supersedes_id}
        filtered = [r for r in filtered if r.entry_id not in superseded_ids]

    result_dicts = []
    for r in filtered:
        result_dicts.append({
            "content": r.content,
            "title": r.title,
            "topic": r.topic,
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
            "threshold_relaxed": too_strict,
            "reranked": not skip_rerank,
        },
    }


# ---------------------------------------------------------------------------
# Ingest entries into Milvus (TOON schema)
# ---------------------------------------------------------------------------

def _build_embedding_text(entry: dict) -> str:
    """Construct embedding text from title + domain_tags + canonical_text."""
    parts = []
    if entry.get("title"):
        parts.append(entry["title"])
    tags = entry.get("domain_tags", [])
    if tags:
        parts.append(f"Topics: {', '.join(tags)}")
    if entry.get("canonical_text"):
        parts.append(entry["canonical_text"])
    return "\n".join(parts)


def _content_hash(text: str) -> str:
    """SHA-256 hash of normalized text for dedup."""
    normalized = " ".join(text.lower().split())
    return hashlib.sha256(normalized.encode()).hexdigest()


async def ingest_entries(entries: list[dict], domain: str = "eng") -> dict:
    """Embed and insert knowledge entries into toon_v2.

    Returns breakdown: {"new": N, "versioned": M, "rejected": K, "skipped_hash": S}.
    new + versioned = successfully inserted rows.
    """
    stats = {"new": 0, "versioned": 0, "rejected": 0, "skipped_hash": 0}
    if not entries:
        return stats

    loop = asyncio.get_running_loop()
    collection = await loop.run_in_executor(None, _get_collection)
    if collection is None:
        logger.error("ingest_entries: collection not available")
        return stats

    now = int(time.time())

    for entry in entries:
        content = entry.get("content", "") or entry.get("canonical_text", "")
        if not content:
            continue

        title = entry.get("title", entry.get("topic", "unknown")).strip()
        tags_raw = entry.get("tags", entry.get("domain_tags", ""))
        if isinstance(tags_raw, str):
            domain_tags = [t.strip() for t in tags_raw.split(",") if t.strip()][:20]
        elif isinstance(tags_raw, list):
            domain_tags = tags_raw[:20]
        else:
            domain_tags = []

        source_url = entry.get("source", entry.get("source_url", "scaffold-engine"))
        source_type = entry.get("source_type", "ai_generated")
        confidence = entry.get("confidence_score", 0.60)

        ch = _content_hash(content)

        # --- Dedup: exact hash check ---
        try:
            existing = await loop.run_in_executor(
                None,
                lambda: collection.query(
                    expr=f'content_hash == "{ch}" and domain == "{domain}"',
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

        embedding_text = _build_embedding_text({
            "title": title,
            "domain_tags": domain_tags,
            "canonical_text": content,
        })

        vector = await _embed_content(embedding_text)
        if vector is None:
            logger.warning("ingest_embed_failed for title=%s", title)
            continue
        # Version chain tracking
        new_version = 1
        new_supersedes = ""

# --- Dedup: semantic similarity check — auto-reject above threshold ---
        try:
            sim_results = await loop.run_in_executor(
                None,
                lambda v=vector: collection.search(
                    data=[v],
                    anns_field="dense_vector",
                    param={"metric_type": "COSINE", "params": {"ef": 32}},
                    limit=1,
                    expr=f'domain == "{domain}"',
                    output_fields=["entry_id", "content_hash", "version", "supersedes_id"],
                ),
            )
            if sim_results and sim_results[0]:
                top_hit = sim_results[0][0]
                sim_score = float(top_hit.score)
                if sim_score > settings.semantic_dedup_threshold and top_hit.entity.get("content_hash") != ch:
                    existing_eid = top_hit.entity.get("entry_id", str(top_hit.id))
                    logger.info(
                        "dedup_rejected: sim=%.4f title='%s' existing='%s'",
                        sim_score, title[:50], existing_eid,
                    )
                    try:
                        async with async_session() as session:
                            await session.execute(
                                text(
                                    "INSERT INTO dedup_log (new_content_hash, existing_entry_id, similarity_score, action_taken) "
                                    "VALUES (:hash, :eid, :score, 'rejected')"
                                ),
                                {"hash": ch, "eid": existing_eid, "score": sim_score},
                            )
                            await session.commit()
                    except Exception as db_err:
                        logger.error("dedup_log_write_failed: %s", db_err)
                    stats["rejected"] += 1
                    continue  # Skip insertion
                elif sim_score >= 0.90:
                    # VERSION CHAIN: same topic, updated content
                    old_entry = top_hit.entity
                    old_version = old_entry.get("version", 1)
                    old_entry_id = old_entry.get("entry_id", str(top_hit.id))
                    new_version = old_version + 1
                    new_supersedes = old_entry_id
                    logger.info(
                        "version_chain_created: v%d supersedes='%s' sim=%.4f title='%s'",
                        new_version, old_entry_id, sim_score, title[:50],
                    )
        except Exception as e:
            logger.debug("semantic_dedup_failed: %s", e)

        topic_slug = title.lower().replace(" ", "-")[:60]
        entry_id = f"scaffold-{topic_slug}-{ch[:8]}"

        row = [{
            "entry_id": entry_id,
            "title": title,
            "canonical_text": content,
            "domain": domain,
            "domain_tags": domain_tags,
            "confidence_score": float(confidence),
            "source_type": source_type,
            "source_url": source_url,
            "content_hash": ch,
            "model_id": settings.model_embedder_id,
            "version": new_version,
            "supersedes_id": new_supersedes,
            "created_at": now,
            "updated_at": now,
            "expires_at": compute_expires_at(source_type, now),
            "dense_vector": vector,
        }]

        try:
            await loop.run_in_executor(
                None, lambda r=row: collection.insert(r)
            )
            if new_supersedes:
                stats["versioned"] += 1
            else:
                stats["new"] += 1
        except Exception as e:
            logger.warning("ingest_insert_failed: %s", e)

    inserted = stats["new"] + stats["versioned"]
    if inserted > 0:
        await loop.run_in_executor(None, collection.flush)
        logger.info(
            "ingested %d (new=%d versioned=%d rejected=%d hash_skipped=%d) into toon_v2",
            inserted, stats["new"], stats["versioned"], stats["rejected"], stats["skipped_hash"],
        )

    return stats
