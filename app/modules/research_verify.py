"""Session-scoped provenance audit (§17.114 + §17.121 —
/research/verify/<session_id>[?recheck=true]).

Given a research session_id, enumerate every Milvus entry it produced
(via the §17.104 + §17.114 ``rag_entry_provenance`` sidecar) and report
their current Milvus state: still present, superseded, or missing.

§17.121 adds an opt-in ``recheck_upstream`` mode: HEAD/GET each entry's
``source_url`` and classify the response as ``reachable``, ``missing``
(404), ``forbidden`` (4xx other than 404), or ``error`` (5xx / timeout /
connection failure). Surfaces "did the source vanish from upstream"
without re-hashing content (full content-drift detection still requires
per-source-type re-normalize and is a §17.121 follow-up).

Returned shape:

    {
        "session_id": "<uuid>",
        "session_meta": {"topic", "status", "completed_at"} | None,
        "totals": {
            "provenance_rows": N, "in_milvus": M, "superseded": S, "missing": X,
            # only when recheck_upstream=True:
            "reachable": R, "upstream_missing": Um, "upstream_error": Ue,
        },
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
                # only when recheck_upstream=True:
                "upstream_state": "reachable" | "missing" | "forbidden" | "error" | "skipped",
                "upstream_status": <int> | None,
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


async def _recheck_one_url(client, url: str) -> dict[str, Any]:
    """HEAD (or GET on 405) the URL and classify the response.

    Returns ``{state, status}``. ``state`` ∈ ``{reachable, missing,
    forbidden, error, skipped}``. ``status`` is the HTTP code or None
    on connection error. No body read — we only care about reachability.
    """
    if not url:
        return {"state": "skipped", "status": None}
    # SSRF re-check — the stored source_url could in principle have been
    # tampered with; rejecting private-IP rebinds here matches the
    # original §17.93 contract on _fetch_url_bounded.
    from app.modules.research_extractors import _is_public_host
    ok, _reason = _is_public_host(url)
    if not ok:
        return {"state": "error", "status": None}
    try:
        r = await client.head(
            url, timeout=10.0,
            headers={"User-Agent": "ScaffoldEngine/1.0 (verify)"},
            follow_redirects=True,
        )
        if r.status_code == 405:
            # Some servers (notably arxiv.org) don't support HEAD —
            # fall back to a GET with the body discarded immediately.
            r = await client.get(
                url, timeout=10.0,
                headers={"User-Agent": "ScaffoldEngine/1.0 (verify)"},
                follow_redirects=True,
            )
        status = r.status_code
    except Exception as e:
        logger.debug("verify_recheck_error: url=%s err=%s", url, e)
        return {"state": "error", "status": None}

    if 200 <= status < 300:
        state = "reachable"
    elif status == 404 or status == 410:
        state = "missing"
    elif 400 <= status < 500:
        state = "forbidden"
    else:
        state = "error"
    return {"state": state, "status": status}


async def _recheck_upstream(
    url_by_eid: dict[str, str], concurrency: int = 5,
) -> dict[str, dict[str, Any]]:
    """Fan out HEAD requests with bounded concurrency.

    Returns ``{entry_id: {state, status}}``. Entries with empty URLs
    map to ``{"state": "skipped", "status": None}``.
    """
    from app.utils.http_clients import get_generic_http_client
    client = get_generic_http_client()
    sem = asyncio.Semaphore(concurrency)

    async def _one(eid: str, url: str):
        async with sem:
            return eid, await _recheck_one_url(client, url)

    results = await asyncio.gather(*(
        _one(eid, url) for eid, url in url_by_eid.items()
    ))
    return dict(results)


async def verify_session(
    db_session: AsyncSession,
    session_id: str,
    *,
    recheck_upstream: bool = False,
) -> dict[str, Any]:
    """Build a verify report for a research session. See module docstring
    for the returned shape.

    Pre-§17.114 sessions have no provenance rows linked by session_id —
    the report returns an empty ``entries`` list and ``provenance_rows=0``.

    ``recheck_upstream=True`` (§17.121) HEAD-requests each entry's
    source_url and classifies the response. Adds ``upstream_state`` per
    entry + ``reachable``/``upstream_missing``/``upstream_error`` totals.
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

    # §17.121 — optional upstream reachability recheck. Fan out HEAD
    # requests with bounded concurrency BEFORE building the per-entry
    # dicts so we can merge results into each entry in one pass.
    recheck_results: dict[str, dict[str, Any]] = {}
    if recheck_upstream:
        url_by_eid = {
            prov["entry_id"]: (milvus_rows.get(prov["entry_id"], {}) or {}).get("source_url", "")
            for prov in provenance_rows
        }
        recheck_results = await _recheck_upstream(url_by_eid)

    entries: list[dict[str, Any]] = []
    n_present = 0
    n_superseded = 0
    n_missing = 0
    n_reachable = 0
    n_upstream_missing = 0
    n_upstream_error = 0
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
        entry = {
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
        }
        if recheck_upstream:
            rc = recheck_results.get(eid) or {"state": "skipped", "status": None}
            entry["upstream_state"] = rc["state"]
            entry["upstream_status"] = rc["status"]
            if rc["state"] == "reachable":
                n_reachable += 1
            elif rc["state"] == "missing":
                n_upstream_missing += 1
            elif rc["state"] in ("forbidden", "error"):
                n_upstream_error += 1
        entries.append(entry)

    totals: dict[str, Any] = {
        "provenance_rows": len(provenance_rows),
        "in_milvus": n_present,
        "superseded": n_superseded,
        "missing": n_missing,
    }
    if recheck_upstream:
        totals["reachable"] = n_reachable
        totals["upstream_missing"] = n_upstream_missing
        totals["upstream_error"] = n_upstream_error

    return {
        "session_id": session_id,
        "session_meta": session_meta,
        "totals": totals,
        "entries": entries,
    }
