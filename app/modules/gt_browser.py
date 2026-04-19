"""
Ground Truth Browser — read-only Milvus queries for TOON v2 entry browsing.

Endpoints: /gt/list, /gt/search, /gt/detail/{entry_id}, /gt/stats.
"""

import asyncio
import logging

from fastapi import HTTPException
from pymilvus import Collection

from app.config import settings
from app.utils.embedding import embed_query
from app.utils.milvus_utils import get_collection

logger = logging.getLogger(__name__)

COLLECTION_NAME = "toon_v2"
OUTPUT_FIELDS = [
    "entry_id", "title", "domain_tags", "canonical_text",
    "source_url", "domain", "confidence_score", "source_type",
    "supersedes_id",
]


def _get_collection() -> Collection:
    """Connect to Milvus and return the collection handle."""
    return get_collection(raise_on_missing=True)  # type: ignore[return-value]


def _supersede_clause(include_history: bool) -> str:
    """Return the Milvus expression fragment that hides superseded entries.

    Milvus expression syntax: `supersedes_id == ""` keeps only originals
    (entries that do not chain off a prior version).
    """
    return "" if include_history else 'supersedes_id == ""'


def _join_expr(*parts: str) -> str:
    """AND-join non-empty expression fragments."""
    return " && ".join(p for p in parts if p)


async def gt_list(
    page: int = 1,
    per_page: int = 20,
    include_history: bool = False,
) -> dict:
    """Paginated list of TOON entries.

    By default hides superseded (version-chained) entries; pass
    ``include_history=True`` to see all versions.
    """
    def _sync() -> dict:
        col = _get_collection()
        offset = (page - 1) * per_page
        total = col.num_entities

        expr = _join_expr("entry_id != ''", _supersede_clause(include_history))

        results = col.query(
            expr=expr,
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
            "include_history": include_history,
            "entries": entries,
        }

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync)


async def gt_search(
    query: str,
    top_k: int = 10,
    domain: str | None = None,
    include_history: bool = False,
) -> dict:
    """Semantic search against TOON entries.

    By default hides superseded (version-chained) entries.
    """
    vector = await embed_query(query)
    if vector is None:
        raise RuntimeError("Empty embedding returned")

    def _sync() -> dict:
        col = _get_collection()
        search_params = {"metric_type": "COSINE", "params": {"ef": 128, "refine_k": 2}}
        expr = _join_expr(
            f'domain == "{domain}"' if domain else "",
            _supersede_clause(include_history),
        ) or None

        results = col.search(
            data=[vector],
            anns_field="dense_vector",
            param=search_params,
            limit=top_k,
            output_fields=OUTPUT_FIELDS,
            expr=expr,
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

        return {
            "query": query,
            "top_k": top_k,
            "include_history": include_history,
            "results": entries,
        }

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync)


async def gt_detail(entry_id: str) -> dict:
    """Full content of a specific TOON entry.

    Raises:
        HTTPException(404): entry not found.
    """
    def _sync() -> dict:
        col = _get_collection()
        results = col.query(
            expr=f'entry_id == "{entry_id}"',
            output_fields=OUTPUT_FIELDS,
        )

        if not results:
            raise HTTPException(
                status_code=404,
                detail=f"entry {entry_id} not found",
            )

        r = results[0]
        tags_list = r.get("domain_tags", [])
        tags_str = ", ".join(tags_list) if isinstance(tags_list, list) else str(tags_list)
        return {
            "found": True,  # retained for backward-compat with UI
            "entry_id": r.get("entry_id", ""),
            "title": r.get("title", ""),
            "domain": r.get("domain", ""),
            "tags": tags_str,
            "content": r.get("canonical_text", ""),
            "source_url": r.get("source_url", ""),
            "confidence": r.get("confidence_score", 0.0),
            "source_type": r.get("source_type", ""),
            "supersedes_id": r.get("supersedes_id", ""),
        }

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync)


async def gt_stats() -> dict:
    """Collection summary: count, domain distribution, tag breakdown.

    Uses paginated scans with ``settings.gt_stats_scan_limit`` per page so
    results aren't silently truncated at PyMilvus' 16384 default. When the
    collection size exceeds the scan budget, ``truncated: true`` is returned.
    """
    def _sync() -> dict:
        col = _get_collection()
        total = col.num_entities
        limit = settings.gt_stats_scan_limit

        domains: dict[str, int] = {}
        tags_dist: dict[str, int] = {}
        sources: dict[str, int] = {}

        scanned = 0
        offset = 0
        max_offset = 16384  # Milvus hard cap on offset + limit per query

        while True:
            if offset + limit > max_offset:
                logger.warning(
                    "gt_stats_scan_capped: offset+limit would exceed Milvus max %d",
                    max_offset,
                )
                break

            page = col.query(
                expr="entry_id != ''",
                output_fields=["title", "domain", "domain_tags", "source_type"],
                limit=limit,
                offset=offset,
            )
            if not page:
                break

            for entry in page:
                d = entry.get("domain", "unknown") or "unknown"
                domains[d] = domains.get(d, 0) + 1

                raw_tags = entry.get("domain_tags", [])
                if isinstance(raw_tags, list):
                    for tag in raw_tags:
                        if tag:
                            tags_dist[tag] = tags_dist.get(tag, 0) + 1

                src = entry.get("source_type", "unknown") or "unknown"
                sources[src] = sources.get(src, 0) + 1

            scanned += len(page)
            last_page_short = len(page) < limit
            offset += limit
            if last_page_short:
                break
        else:
            # Loop exited via the offset cap (not via "short page" break)
            pass

        # Only truncated if we bailed due to the offset cap, not natural exhaustion
        truncated = (offset + limit > max_offset) and not last_page_short if scanned > 0 else False
        if truncated:
            logger.warning(
                "gt_stats_truncated: scanned=%d of total=%d", scanned, total,
            )

        return {
            "total_entries": total,
            "scanned": scanned,
            "truncated": truncated,
            "domains": dict(sorted(domains.items(), key=lambda x: -x[1])),
            "tags": dict(sorted(tags_dist.items(), key=lambda x: -x[1])),
            "source_types": dict(sorted(sources.items(), key=lambda x: -x[1])),
        }

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync)
