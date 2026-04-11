"""
Step 19: Ground Truth Browser — Orchestrator Module
Read-only Milvus queries for TOON entry browsing.
Endpoints: /gt/list, /gt/search, /gt/detail/{entry_id}, /gt/stats
"""

import asyncio
import logging
from typing import Optional

from pymilvus import connections, Collection, utility

from app import model_router
from app.config import settings

logger = logging.getLogger(__name__)

COLLECTION_NAME = "technical_knowledge"
OUTPUT_FIELDS = ["entry_id", "topic", "tags", "content", "source_file", "source_url"]
EMBED_MODEL = "qwen3-embedding:8b"


def _get_collection() -> Collection:
    """Connect to Milvus and return the collection handle."""
    try:
        utility.list_collections()
    except Exception:
        connections.connect(alias="default", uri=settings.milvus_uri)

    if not utility.has_collection(COLLECTION_NAME):
        raise RuntimeError(f"Collection '{COLLECTION_NAME}' not found in Milvus")
    col = Collection(COLLECTION_NAME)
    col.load()
    return col


async def gt_list(page: int = 1, per_page: int = 20) -> dict:
    """Paginated list of all TOON entries."""
    def _sync() -> dict:
        col = _get_collection()
        offset = (page - 1) * per_page
        total = col.num_entities

        results = col.query(
            expr="entry_id != ''",
            output_fields=OUTPUT_FIELDS,
            limit=per_page,
            offset=offset,
        )

        entries = []
        for r in results:
            content = r.get("content", "") or ""
            entries.append({
                "entry_id": r.get("entry_id", ""),
                "topic": r.get("topic", ""),
                "tags": r.get("tags", ""),
                "snippet": content[:120] + "…" if len(content) > 120 else content,
                "source_file": r.get("source_file", ""),
            })

        return {
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": (total + per_page - 1) // per_page,
            "entries": entries,
        }

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync)


async def gt_search(query: str, top_k: int = 10) -> dict:
    """Semantic search against TOON entries."""
    # Embedding is already async — do it on the main road
    vectors = await model_router.embed(query, model=EMBED_MODEL)
    if not vectors or not vectors[0]:
        raise RuntimeError("Empty embedding returned")
    vector = vectors[0]

    def _sync() -> dict:
        col = _get_collection()
        search_params = {"metric_type": "L2", "params": {"ef": 128}}
        results = col.search(
            data=[vector],
            anns_field="vector",
            param=search_params,
            limit=top_k,
            output_fields=OUTPUT_FIELDS,
        )

        entries = []
        for hits in results:
            for hit in hits:
                entity = hit.entity
                entries.append({
                    "entry_id": entity.get("entry_id", ""),
                    "topic": entity.get("topic", ""),
                    "tags": entity.get("tags", ""),
                    "snippet": (entity.get("content", "") or "")[:200],
                    "score": round(hit.score, 4),
                    "source_file": entity.get("source_file", ""),
                })

        return {"query": query, "top_k": top_k, "results": entries}

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync)


async def gt_detail(entry_id: str) -> dict:
    """Full content of a specific TOON entry."""
    def _sync() -> dict:
        col = _get_collection()
        results = col.query(
            expr=f'entry_id == "{entry_id}"',
            output_fields=OUTPUT_FIELDS,
        )

        if not results:
            return {"found": False, "entry_id": entry_id}

        r = results[0]
        return {
            "found": True,
            "entry_id": r.get("entry_id", ""),
            "topic": r.get("topic", ""),
            "tags": r.get("tags", ""),
            "content": r.get("content", ""),
            "source_file": r.get("source_file", ""),
            "source_url": r.get("source_url", ""),
        }

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync)


async def gt_stats() -> dict:
    """Collection summary: count, topic distribution, tag breakdown."""
    def _sync() -> dict:
        col = _get_collection()
        total = col.num_entities

        all_entries = col.query(
            expr="entry_id != ''",
            output_fields=["topic", "tags", "source_file"],
            limit=16384,
        )

        topics: dict[str, int] = {}
        tags_dist: dict[str, int] = {}
        sources: dict[str, int] = {}

        for entry in all_entries:
            topic = entry.get("topic", "unknown") or "unknown"
            topics[topic] = topics.get(topic, 0) + 1

            raw_tags = entry.get("tags", "") or ""
            for tag in raw_tags.split(","):
                tag = tag.strip()
                if tag:
                    tags_dist[tag] = tags_dist.get(tag, 0) + 1

            src = entry.get("source_file", "unknown") or "unknown"
            sources[src] = sources.get(src, 0) + 1

        return {
            "total_entries": total,
            "topics": dict(sorted(topics.items(), key=lambda x: -x[1])),
            "tags": dict(sorted(tags_dist.items(), key=lambda x: -x[1])),
            "source_files": dict(sorted(sources.items(), key=lambda x: -x[1])),
        }

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync)
