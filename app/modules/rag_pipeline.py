"""Scaffold Engine -- RAG pipeline module.

Query flow:
  1. Embed query (qwen3-embedding:8b, 4096d)
  2. Milvus ANN search (vector similarity)
  3. Keyword search (content/topic filter)
  4. RRF fusion of both result sets
  5. Cross-encoder rerank (Qwen3-Reranker-0.6B via sentence-transformers)
  6. Confidence filter + dynamic top-k

Step 13 of 23-step build plan.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from pymilvus import Collection, connections, utility
from app.rerankers import rerank as cross_encoder_rerank

from app import model_router
from app.config import settings

logger = logging.getLogger("scaffold.rag")

COLLECTION_NAME = "technical_knowledge"
EMBED_DIM = 4096
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


# ---------------------------------------------------------------------------
# Milvus connection helper
# ---------------------------------------------------------------------------

def _get_collection() -> Collection | None:
    """Get Milvus collection, connecting if needed."""
    try:
        # Ensure connected
        try:
            utility.list_collections()
        except Exception:
            connections.connect(alias="default", uri=settings.milvus_uri)

        if not utility.has_collection(COLLECTION_NAME):
            logger.error("Collection '%s' not found in Milvus", COLLECTION_NAME)
            return None

        col = Collection(COLLECTION_NAME)
        col.load()
        return col
    except Exception as e:
        logger.error("Failed to get Milvus collection: %s", e)
        return None


# ---------------------------------------------------------------------------
# Vector search
# ---------------------------------------------------------------------------

async def _vector_search(
    collection: Collection,
    query_embedding: list[float],
    top_k: int,
    domain: str | None = None,
) -> list[RagResult]:
    """ANN search in Milvus using query embedding."""
    try:
        search_kwargs = dict(
            data=[query_embedding],
            anns_field="vector",
            param={"metric_type": "L2", "params": {"nprobe": 16}},
            limit=top_k,
            output_fields=["content", "topic", "tags", "source_file", "source_url", "entry_id", "domain"],
        )
        if domain:
            search_kwargs["expr"] = f'domain == "{domain}"'
        results = collection.search(**search_kwargs)

        hits = []
        for hit in results[0]:
            entity = hit.entity
            hits.append(RagResult(
                content=entity.get("content", ""),
                topic=entity.get("topic", ""),
                tags=entity.get("tags", ""),
                source_file=entity.get("source_file", ""),
                source_url=entity.get("source_url", ""),
                entry_id=entity.get("entry_id", ""),
                domain=entity.get("domain", ""),
                vector_score=float(hit.score),
            ))
        return hits
    except Exception as e:
        # Fall back to L2 if COSINE fails (older index)
        logger.warning("COSINE search failed, trying L2: %s", e)
        try:
            l2_kwargs = dict(
                data=[query_embedding],
                anns_field="vector",
                param={"metric_type": "L2", "params": {"nprobe": 16}},
                limit=top_k,
                output_fields=["content", "topic", "tags", "source_file", "source_url", "entry_id", "domain"],
            )
            if domain:
                l2_kwargs["expr"] = f'domain == "{domain}"'
            results = collection.search(**l2_kwargs)
            hits = []
            for hit in results[0]:
                entity = hit.entity
                # Convert L2 distance to similarity (lower distance = higher similarity)
                similarity = 1.0 / (1.0 + float(hit.score))
                hits.append(RagResult(
                    content=entity.get("content", ""),
                    topic=entity.get("topic", ""),
                    tags=entity.get("tags", ""),
                    source_file=entity.get("source_file", ""),
                    source_url=entity.get("source_url", ""),
                    entry_id=entity.get("entry_id", ""),
                    domain=entity.get("domain", ""),
                    vector_score=similarity,
                ))
            return hits
        except Exception as e2:
            logger.error("Vector search failed: %s", e2)
            return []


# ---------------------------------------------------------------------------
# Keyword search (content/topic filter via Milvus expressions)
# ---------------------------------------------------------------------------

async def _keyword_search(
    collection: Collection,
    query: str,
    top_k: int,
    domain: str | None = None,
) -> list[RagResult]:
    """Keyword-based search using Milvus query expressions."""
    # Extract meaningful keywords (>= 3 chars, not stopwords)
    stopwords = {"the", "and", "for", "are", "but", "not", "you", "all", "can", "has", "was", "one", "our", "out",
                 "how", "what", "when", "where", "which", "who", "why", "with", "this", "that", "from", "into"}
    words = [w.strip().lower() for w in query.split() if len(w.strip()) >= 3 and w.strip().lower() not in stopwords]

    if not words:
        return []

    # Build OR expression for content and topic fields
    conditions = []
    for word in words[:5]:  # Limit to 5 keywords
        safe_word = word.replace("'", "\\'").replace('"', '\\"')
        conditions.append(f'content like "%{safe_word}%"')
        conditions.append(f'topic like "%{safe_word}%"')

    expr = " or ".join(conditions)
    if domain:
        expr = f'domain == "{domain}" and ({expr})'

    try:
        results = collection.query(
            expr=expr,
            output_fields=["content", "topic", "tags", "source_file", "source_url", "entry_id", "domain"],
            limit=top_k,
        )

        hits = []
        for r in results:
            # Score by keyword match count
            content_lower = r.get("content", "").lower()
            topic_lower = r.get("topic", "").lower()
            match_count = sum(1 for w in words if w in content_lower or w in topic_lower)
            score = match_count / len(words) if words else 0.0

            hits.append(RagResult(
                content=r.get("content", ""),
                topic=r.get("topic", ""),
                tags=r.get("tags", ""),
                source_file=r.get("source_file", ""),
                source_url=r.get("source_url", ""),
                entry_id=r.get("entry_id", ""),
                domain=r.get("domain", ""),
                keyword_score=score,
            ))
        return hits
    except Exception as e:
        logger.warning("Keyword search failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# RRF fusion
# ---------------------------------------------------------------------------

def _rrf_fuse(
    vector_results: list[RagResult],
    keyword_results: list[RagResult],
    k: int = RRF_K,
) -> list[RagResult]:
    """Reciprocal Rank Fusion of vector and keyword result sets."""
    # Index by content (dedup key)
    merged: dict[str, RagResult] = {}

    # Score vector results by rank
    for rank, result in enumerate(vector_results):
        key = result.content[:200]  # Use content prefix as dedup key
        if key not in merged:
            merged[key] = result
        merged[key].rrf_score += 1.0 / (k + rank + 1)
        merged[key].vector_score = max(merged[key].vector_score, result.vector_score)

    # Score keyword results by rank
    for rank, result in enumerate(sorted(keyword_results, key=lambda r: r.keyword_score, reverse=True)):
        key = result.content[:200]
        if key not in merged:
            merged[key] = result
        merged[key].rrf_score += 1.0 / (k + rank + 1)
        merged[key].keyword_score = max(merged[key].keyword_score, result.keyword_score)

    # Sort by RRF score descending
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

    # Extract texts for reranker (cap at 20)
    docs = [r.content[:500] for r in results[:20]]

    # Run reranker (sync — CrossEncoder is CPU-bound, not async)
    rr = cross_encoder_rerank(query, docs, top_k=len(docs))

    # Map scores back to RagResult objects
    score_map = {item.index: item.score for item in rr.items}
    for i, r in enumerate(results[:20]):
        if i in score_map:
            r.rerank_score = score_map[i]
            r.final_score = score_map[i]
        else:
            r.rerank_score = r.rrf_score
            r.final_score = r.rrf_score

    # Entries beyond top 20 keep RRF scores
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
) -> dict[str, Any]:
    """Full RAG pipeline: embed -> search -> fuse -> rerank -> filter.

    Args:
        query: Natural language query
        top_k: Maximum results to return
        confidence_threshold: Minimum final score to include
        skip_rerank: If True, skip cross-encoder reranking (faster)

    Returns:
        Dict with results, scores, and metadata
    """
    t0 = time.monotonic()

    # 1. Get collection
    collection = _get_collection()
    if collection is None:
        return {
            "status": "error",
            "error": f"Collection '{COLLECTION_NAME}' not available",
            "results": [],
        }

    # 2. Embed query
    embeddings = await model_router.embed(query, model=settings.model_embedder_pipeline)
    if not embeddings or not embeddings[0]:
        return {
            "status": "error",
            "error": "Failed to generate query embedding",
            "results": [],
        }
    query_embedding = embeddings[0]

    # 3. Parallel search: vector + keyword
    vector_results = await _vector_search(collection, query_embedding, top_k * 2, domain=domain)
    keyword_results = await _keyword_search(collection, query, top_k * 2, domain=domain)

    logger.info(
        "Search: %d vector hits, %d keyword hits for '%s'",
        len(vector_results), len(keyword_results), query[:50],
    )

    # 4. RRF fusion
    fused = _rrf_fuse(vector_results, keyword_results)

    # 5. Rerank (optional)
    if skip_rerank or not fused:
        for r in fused:
            r.final_score = r.rrf_score
        ranked = fused[:top_k]
    else:
        ranked = await _rerank(query, fused, top_k)

    # 6. Confidence filter
    filtered = [r for r in ranked if r.final_score >= confidence_threshold]

    # If filter is too aggressive, return top results anyway with a warning
    too_strict = len(filtered) == 0 and len(ranked) > 0
    if too_strict:
        filtered = ranked[:3]

    latency_ms = round((time.monotonic() - t0) * 1000, 1)
    top_score = round(filtered[0].final_score, 4) if filtered else 0.0
    logger.info(
        "retrieval_complete",
        extra=dict(
            query=query[:200],
            domain=domain or "all",
            n_results=len(filtered),
            top_score=top_score,
            latency_ms=latency_ms,
        ),
    )

    # 7. Build response
    result_dicts = []
    for r in filtered:
        result_dicts.append({
            "content": r.content,
            "topic": r.topic,
            "tags": r.tags,
            "source_file": r.source_file,
            "source_url": r.source_url,
            "entry_id": r.entry_id,
            "domain": r.domain,
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
