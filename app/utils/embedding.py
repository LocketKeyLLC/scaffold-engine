"""Public embedding helpers — decoupled from rag_pipeline internals.

Shared by rag_pipeline.query_rag, gt_extractor, and gt_browser so none of
them reach into private rag_pipeline symbols.
"""
from __future__ import annotations

import logging

from app import model_router
from app.config import settings
from app.utils.embedding_cache import get_cache, truncate_and_normalize

logger = logging.getLogger(__name__)

_QUERY_INSTRUCTION = (
    "Instruct: Given a query, retrieve relevant knowledge entries\nQuery: "
)


async def embed_query(query: str) -> list[float] | None:
    """Embed a query with instruction prefix, MRL truncation, and cache.

    Returns a 512d unit-norm vector, or None on embedder failure.
    """
    query_text = f"{_QUERY_INSTRUCTION}{query}"

    cache = get_cache()
    cached = await cache.get(query_text)
    if cached:
        return cached

    embeddings = await model_router.embed(
        query_text, model=settings.model_embedder_pipeline
    )
    if not embeddings or not embeddings[0]:
        logger.warning("embed_query: empty embedding for query=%r", query[:60])
        return None

    truncated = truncate_and_normalize(embeddings[0])
    await cache.put(query_text, truncated)
    return truncated
