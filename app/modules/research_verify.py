"""Session-scoped provenance audit (§17.114 — /research/verify/<session_id>).

Given a research session_id, enumerate every Milvus entry it produced
(via the §17.104 + §17.114 ``rag_entry_provenance`` sidecar) and report
their current Milvus state: still present, superseded, or missing.

Does NOT re-fetch upstream content. True content-drift detection (re-hash
the upstream body and compare to the ingested ``content_hash``) is a
follow-up — each source_type would need a back-pointer into its
producer's fetcher, which doesn't exist as a generic API yet.

Returned shape:

    {
        "session_id": "<uuid>",
        "session_meta": {"topic", "status", "completed_at"} | None,
        "totals": {"provenance_rows": N, "in_milvus": M, "superseded": S, "missing": X},
        "entries": [
            {
                "entry_id": "...",
                "source_ref": "...",
                "source_url": "...",
                "source_type": "...",
                "fetched_at": <epoch>,
                "quality_signal": {...},
                "in_milvus": bool,
                "milvus_state": "present" | "superseded" | "missing",
                "current_version": int | None,
                "superseded_by": str | None,
                "content_hash_at_ingest": str | None,
            },
            ...
        ],
    }
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import VALID_DOMAINS
from app.modules.provenance import get_provenance_for_session
from app.utils.milvus_utils import get_collection

logger = logging.getLogger("scaffold.research.verify")

_MILVUS_COLLECTION = "toon_v2"


def _quote_id(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _milvus_lookup_entries(entry_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Return {entry_id: {domain, source_type, source_url, content_hash,
    version, supersedes_id}} for every entry_id present in Milvus.

    Fans out one query per VALID_DOMAINS — Milvus 2.5 partition-key
    isolation rejects ``IN`` exprs over the partition key, so we hit
    each partition with the same ``entry_id in [...]`` filter and merge
    results. Empty partitions return zero rows; no harm.
    """
    if not entry_ids:
        return {}
    collection = get_collection()
    if collection is None:
        logger.warning("milvus_unavailable_in_verify")
        return {}

    quoted = [_quote_id(eid) for eid in entry_ids]
    id_filter = f"entry_id in [{','.join(quoted)}]"
    fields = ["entry_id", "domain", "source_type", "source_url",
              "content_hash", "version", "supersedes_id"]
    out: dict[str, dict[str, Any]] = {}
    for d in sorted(VALID_DOMAINS):
        try:
            rows = collection.query(
                expr=f'domain == "{d}" and ({id_filter})',
                output_fields=fields,
                limit=len(entry_ids) + 10,
            )
        except Exception as e:
            logger.debug("milvus_verify_query_failed: domain=%s err=%s", d, e)
            continue
        for r in rows or []:
            eid = r.get("entry_id")
            if eid:
                out[eid] = {
                    "domain": r.get("domain", ""),
                    "source_type": r.get("source_type", ""),
                    "source_url": r.get("source_url", ""),
                    "content_hash": r.get("content_hash", ""),
                    "version": int(r.get("version", 1)),
                    "supersedes_id": r.get("supersedes_id", "") or None,
                }
    return out


def _milvus_lookup_supersedors(entry_ids: list[str]) -> dict[str, str]:
    """Return ``{old_entry_id: new_entry_id}`` for every entry that has
    been superseded by a newer row.

    Implementation mirrors ``_lookup_superseded`` in rag_pipeline but
    we fan out across partitions and capture the new ``entry_id``, not
    just the superseded one — verify wants the forward pointer.
    """
    if not entry_ids:
        return {}
    collection = get_collection()
    if collection is None:
        return {}
    quoted = [_quote_id(eid) for eid in entry_ids]
    expr_id = f"supersedes_id in [{','.join(quoted)}]"
    out: dict[str, str] = {}
    for d in sorted(VALID_DOMAINS):
        try:
            rows = collection.query(
                expr=f'domain == "{d}" and ({expr_id})',
                output_fields=["entry_id", "supersedes_id"],
                limit=len(entry_ids) * 2 + 10,
            )
        except Exception as e:
            logger.debug("milvus_verify_supersede_query_failed: domain=%s err=%s", d, e)
            continue
        for r in rows or []:
            old = r.get("supersedes_id")
            new = r.get("entry_id")
            if old and new:
                out[old] = new
    return out


async def verify_session(
    db_session: AsyncSession,
    session_id: str,
) -> dict[str, Any]:
    """Build a verify report for a research session. See module docstring
    for the returned shape.

    Pre-§17.114 sessions have no provenance rows linked by session_id —
    the report returns an empty ``entries`` list and ``provenance_rows=0``.
    """
    meta_row = await db_session.execute(
        text(
            "SELECT topic, status, completed_at "
            "FROM research_sessions WHERE id = CAST(:sid AS uuid)"
        ),
        {"sid": session_id},
    )
    meta_mapping = meta_row.mappings().first()
    session_meta = None
    if meta_mapping:
        session_meta = {
            "topic": meta_mapping["topic"],
            "status": meta_mapping["status"],
            "completed_at": (
                meta_mapping["completed_at"].isoformat()
                if meta_mapping["completed_at"] else None
            ),
        }

    provenance_rows = await get_provenance_for_session(db_session, session_id)
    entry_ids = [r["entry_id"] for r in provenance_rows]

    loop = asyncio.get_running_loop()
    milvus_rows = await loop.run_in_executor(None, _milvus_lookup_entries, entry_ids)
    supersedors = await loop.run_in_executor(None, _milvus_lookup_supersedors, entry_ids)

    entries: list[dict[str, Any]] = []
    n_present = 0
    n_superseded = 0
    n_missing = 0
    for prov in provenance_rows:
        eid = prov["entry_id"]
        milvus = milvus_rows.get(eid)
        superseded_by = supersedors.get(eid)
        if not milvus:
            state = "missing"
            n_missing += 1
        elif superseded_by:
            state = "superseded"
            n_superseded += 1
        else:
            state = "present"
            n_present += 1
        entries.append({
            "entry_id": eid,
            "source_ref": prov["source_ref"],
            "source_url": (milvus or {}).get("source_url", ""),
            "source_type": (milvus or {}).get("source_type", ""),
            "fetched_at": prov["fetched_at"],
            "quality_signal": prov["quality_signal"],
            "in_milvus": milvus is not None,
            "milvus_state": state,
            "current_version": (milvus or {}).get("version"),
            "superseded_by": superseded_by,
            "content_hash_at_ingest": (milvus or {}).get("content_hash", "") or None,
        })

    return {
        "session_id": session_id,
        "session_meta": session_meta,
        "totals": {
            "provenance_rows": len(provenance_rows),
            "in_milvus": n_present,
            "superseded": n_superseded,
            "missing": n_missing,
        },
        "entries": entries,
    }
