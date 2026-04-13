"""
Step 19: Ground Truth Browser — Orchestrator Module
Read-only Milvus queries for TOON v2 entry browsing.
Endpoints: /gt/list, /gt/search, /gt/detail/{entry_id}, /gt/stats
"""

import asyncio
import logging
from typing import Optional

from pymilvus import connections, Collection, utility

from app.config import settings
from app.modules.rag_pipeline import _embed_query

logger = logging.getLogger(__name__)

COLLECTION_NAME = "toon_v2"
OUTPUT_FIELDS = ["entry_id", "title", "domain_tags", "canonical_text",
                 "source_url", "domain", "confidence_score", "source_type"]


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
            content = r.get("canonical_text", "") or ""
            tags_list = r.get("domain_tags", [])
            tags_str = ", ".join(tags_list) if isinstance(tags_list, list) else str(tags_list)
            entries.append({
                "entry_id": r.get("entry_id", ""),
                "title": r.get("title", ""),
                "domain": r.get("domain", ""),
                "tags": tags_str,
                "snippet": content[:120] + "…" if len(content) > 120 else content,
                "confidence": r.get("confidence_score", 0.0),
                "source_type": r.get("source_type", ""),
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
    vector = await _embed_query(query)
    if vector is None:
        raise RuntimeError("Empty embedding returned")

    def _sync() -> dict:
        col = _get_collection()
        search_params = {"metric_type": "COSINE", "params": {"ef": 128, "refine_k": 2}}
        results = col.search(
            data=[vector],
            anns_field="dense_vector",
            param=search_params,
            limit=top_k,
            output_fields=OUTPUT_FIELDS,
        )

        entries = []
        for hits in results:
            for hit in hits:
                entity = hit.entity
                tags_list = entity.get("domain_tags", [])
                tags_str = ", ".join(tags_list) if isinstance(tags_list, list) else str(tags_list)
                entries.append({
                    "entry_id": entity.get("entry_id", ""),
                    "title": entity.get("title", ""),
                    "domain": entity.get("domain", ""),
                    "tags": tags_str,
                    "snippet": (entity.get("canonical_text", "") or "")[:200],
                    "score": round(hit.score, 4),
                    "confidence": entity.get("confidence_score", 0.0),
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
        tags_list = r.get("domain_tags", [])
        tags_str = ", ".join(tags_list) if isinstance(tags_list, list) else str(tags_list)
        return {
            "found": True,
            "entry_id": r.get("entry_id", ""),
            "title": r.get("title", ""),
            "domain": r.get("domain", ""),
            "tags": tags_str,
            "content": r.get("canonical_text", ""),
            "source_url": r.get("source_url", ""),
            "confidence": r.get("confidence_score", 0.0),
            "source_type": r.get("source_type", ""),
        }

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync)


async def gt_stats() -> dict:
    """Collection summary: count, domain distribution, tag breakdown."""
    def _sync() -> dict:
        col = _get_collection()
        total = col.num_entities

        all_entries = col.query(
            expr="entry_id != ''",
            output_fields=["title", "domain", "domain_tags", "source_type"],
            limit=16384,
        )

        domains: dict[str, int] = {}
        tags_dist: dict[str, int] = {}
        sources: dict[str, int] = {}

        for entry in all_entries:
            domain = entry.get("domain", "unknown") or "unknown"
            domains[domain] = domains.get(domain, 0) + 1

            raw_tags = entry.get("domain_tags", [])
            if isinstance(raw_tags, list):
                for tag in raw_tags:
                    if tag:
                        tags_dist[tag] = tags_dist.get(tag, 0) + 1

            src = entry.get("source_type", "unknown") or "unknown"
            sources[src] = sources.get(src, 0) + 1

        return {
            "total_entries": total,
            "domains": dict(sorted(domains.items(), key=lambda x: -x[1])),
            "tags": dict(sorted(tags_dist.items(), key=lambda x: -x[1])),
            "source_types": dict(sorted(sources.items(), key=lambda x: -x[1])),
        }

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync)
