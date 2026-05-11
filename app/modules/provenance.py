"""Provenance + confidence helpers for RAG entries.

Phase-1 deep-search producers (GitHub deep, HF, SO/HN/arXiv,
Reddit-allowlisted, Wikipedia) record provenance via ``build_provenance``
+ pass it through ``ingest_entries``; ``query_rag`` attaches it to each
returned result so callers can verify ground truth.

Provenance shape:
    {"source_ref": <SHA/tag/post-id>, "fetched_at": <epoch_seconds>,
     "quality_signal": <dict>}

Confidence: derived from ``source_type`` via ``CONFIDENCE_BY_SOURCE``.
Producers may override with an explicit value when they have
finer-grained signal than the source_type alone.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("scaffold.provenance")


CONFIDENCE_BY_SOURCE: dict[str, float] = {
    "test_code": 1.0,
    "release_notes": 0.95,
    "ci_config": 0.95,
    "model_card": 0.90,
    "dataset_card": 0.90,
    "paper_abstract": 0.85,
    "so_answer": 0.85,
    "official_docs": 0.85,
    "curated": 0.85,
    "tech_docs": 0.80,
    "wiki_article": 0.75,
    "hn_comment": 0.65,
    "community": 0.60,
    "reddit_post": 0.60,
    "ai_generated": 0.55,
    "real_time": 0.50,
    "news": 0.50,
    # §17.125 — disputed_claim ingest. LOW confidence so retrieval can
    # warn callers that the content was downvoted / locked / withdrawn.
    # Below the §17.108 quality gates' default acceptance threshold,
    # not below it so far it's filtered before retrieval — that'd
    # defeat the purpose of recording negative knowledge.
    "disputed_claim": 0.30,
}
DEFAULT_CONFIDENCE: float = 0.60


def confidence_for(source_type: str, override: float | None = None) -> float:
    """Return confidence_score for an entry.

    Explicit ``override`` (caller-supplied) always wins; otherwise look up
    in ``CONFIDENCE_BY_SOURCE``. Unknown source_types fall back to
    ``DEFAULT_CONFIDENCE`` with a warning log line.
    """
    if override is not None:
        return float(override)
    if source_type not in CONFIDENCE_BY_SOURCE:
        logger.warning(
            "confidence_unknown_source_type: source_type=%r falling_back_to=%.2f",
            source_type, DEFAULT_CONFIDENCE,
        )
        return DEFAULT_CONFIDENCE
    return CONFIDENCE_BY_SOURCE[source_type]


def build_provenance(
    source_ref: str = "",
    fetched_at: int | None = None,
    quality_signal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Construct a provenance dict for storage.

    ``fetched_at=None`` → ``int(time.time())``. ``quality_signal=None`` →
    ``{}``. The producer should set both whenever the upstream API
    surfaces them (e.g., SO votes, HF revision SHA).
    """
    return {
        "source_ref": source_ref,
        "fetched_at": fetched_at if fetched_at is not None else int(time.time()),
        "quality_signal": quality_signal or {},
    }


async def write_provenance(
    session: AsyncSession,
    entry_id: str,
    provenance: dict[str, Any],
    session_id: str | None = None,
) -> None:
    """Insert (or replace) the provenance row for a freshly-upserted entry.

    Mirrors Milvus's ``upsert(keys=[entry_id])`` semantic — re-ingest of
    the same entry overwrites the provenance row. Caller commits.

    ``session_id`` is optional — when set, the row is linked to a
    research session (column added in migration 035, §17.114). Direct
    ingest paths (``/rag/ingest``) leave it ``NULL``, which keeps those
    entries correctly invisible to ``/research/verify/{session_id}``.
    """
    await session.execute(
        text(
            "INSERT INTO rag_entry_provenance "
            "(entry_id, source_ref, fetched_at, quality_signal, session_id) "
            "VALUES (:eid, :ref, :fa, CAST(:qs AS jsonb), CAST(:sid AS uuid)) "
            "ON CONFLICT (entry_id) DO UPDATE SET "
            "source_ref = EXCLUDED.source_ref, "
            "fetched_at = EXCLUDED.fetched_at, "
            "quality_signal = EXCLUDED.quality_signal, "
            "session_id = EXCLUDED.session_id"
        ),
        {
            "eid": entry_id,
            "ref": str(provenance.get("source_ref", "")),
            "fa": int(provenance.get("fetched_at", time.time())),
            "qs": json.dumps(provenance.get("quality_signal", {})),
            "sid": str(session_id) if session_id else None,
        },
    )


async def get_provenance_for_session(
    session: AsyncSession,
    session_id: str,
) -> list[dict[str, Any]]:
    """Return every provenance row for a research session.

    Used by ``/research/verify/{session_id}`` to enumerate the entries
    ingested by a session and re-fetch their upstream content.
    Returns a list of dicts: ``{entry_id, source_ref, fetched_at,
    quality_signal}`` — same shape as ``get_provenance_batch`` per-entry.
    Empty list when no rows match (unknown session OR session predated §17.114).
    """
    result = await session.execute(
        text(
            "SELECT entry_id, source_ref, fetched_at, quality_signal "
            "FROM rag_entry_provenance "
            "WHERE session_id = CAST(:sid AS uuid) "
            "ORDER BY fetched_at"
        ),
        {"sid": str(session_id)},
    )
    out: list[dict[str, Any]] = []
    for row in result.mappings():
        qs = row["quality_signal"]
        out.append({
            "entry_id": row["entry_id"],
            "source_ref": row["source_ref"],
            "fetched_at": int(row["fetched_at"]),
            "quality_signal": qs if isinstance(qs, dict) else (json.loads(qs) if qs else {}),
        })
    return out


async def get_provenance_batch(
    session: AsyncSession,
    entry_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """Batch-fetch provenance for query_rag result attachment.

    Returns ``{entry_id: {source_ref, fetched_at, quality_signal}}``.
    Missing entry_ids are absent from the map — callers must treat
    "no provenance row" as "ingested before §17.106 producer rollout".
    """
    if not entry_ids:
        return {}
    result = await session.execute(
        text(
            "SELECT entry_id, source_ref, fetched_at, quality_signal "
            "FROM rag_entry_provenance WHERE entry_id = ANY(:eids)"
        ),
        {"eids": entry_ids},
    )
    out: dict[str, dict[str, Any]] = {}
    for row in result.mappings():
        qs = row["quality_signal"]
        out[row["entry_id"]] = {
            "source_ref": row["source_ref"],
            "fetched_at": int(row["fetched_at"]),
            "quality_signal": qs if isinstance(qs, dict) else (json.loads(qs) if qs else {}),
        }
    return out
