"""Public embedding helpers — decoupled from rag_pipeline internals.

Shared by rag_pipeline.query_rag, gt_extractor, and gt_browser so none of
them reach into private rag_pipeline symbols.
"""
from __future__ import annotations

import logging

from app import model_router
from app.utils.embedding_cache import get_cache, truncate_and_normalize

logger = logging.getLogger(__name__)

# §17.118 — per-intent query instruction templates. Different retrieval
# intents (code lookup, Q&A, research papers) benefit from instruction
# prefixes that match the embedding-space neighborhood the caller wants.
# Cache keys include the full prefixed text, so different intents map
# to different cache entries — no cross-intent contamination.
EMBED_QUERY_TEMPLATES: dict[str, str] = {
    "general": "Instruct: Given a query, retrieve relevant knowledge entries\nQuery: ",
    "code": "Instruct: Given a query, retrieve code examples and snippets demonstrating the API or behavior asked about\nQuery: ",
    "qa": "Instruct: Given a question, retrieve community-validated answers and discussions\nQuery: ",
    "paper": "Instruct: Given a research query, retrieve relevant paper abstracts and academic content\nQuery: ",
}
# Back-compat alias — pre-§17.118 callers (and the cache they populated)
# used this name; new code should reference EMBED_QUERY_TEMPLATES["general"].
_QUERY_INSTRUCTION = EMBED_QUERY_TEMPLATES["general"]


async def embed_query(
    query: str,
    *,
    query_intent: str = "general",
) -> list[float] | None:
    """Embed a query with instruction prefix, MRL truncation, and cache.

    ``query_intent`` selects the instruction template — see
    ``EMBED_QUERY_TEMPLATES`` for the supported intents. Unknown intents
    fall back to ``"general"`` with a debug log line (validation belongs
    at the API boundary; ``embed_query`` stays permissive).

    Returns a 512d unit-norm vector, or None on embedder failure.
    """
    template = EMBED_QUERY_TEMPLATES.get(query_intent)
    if template is None:
        logger.debug(
            "embed_query_unknown_intent: %r falling_back_to=general",
            query_intent,
        )
        template = EMBED_QUERY_TEMPLATES["general"]
    query_text = f"{template}{query}"

    cache = get_cache()
    cached = await cache.get(query_text)
    if cached:
        return cached

    embeddings = await model_router.embed(
        query_text, role="model_embedder_pipeline",
    )
    if not embeddings or not embeddings[0]:
        logger.warning("embed_query: empty embedding for query=%r", query[:60])
        return None

    truncated = truncate_and_normalize(embeddings[0])
    await cache.put(query_text, truncated)
    return truncated
