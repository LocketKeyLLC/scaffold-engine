"""§17.1437 — what a step's research found stays found.

Every guide and fix pass researched its step from scratch, so a page that
answered the step on one pass was gone on the next whenever the web search
behind it had a bad minute. On 2026-10-09 google cse, brave and startpage were
suspended and bing returned only homepages: ADD4's router walkthrough had no
source and asked the operator to describe the My Spectrum app again, after
§17.1435-1436 had taught it to give the researched click path.

`keep_and_recall` is called with a pass's fresh sources. It keeps the web pages
worth keeping (a real page with content, not a site's front door) for this
session and step, and returns the fresh sources plus the step's earlier pages
that this pass did not find again — so the click path a source gave once is in
every later pass's research block. Optional work: it runs in a savepoint and
returns the fresh sources unchanged on any failure.
"""
from __future__ import annotations

import logging
from typing import Optional

from app.utils.savepoint import savepoint

logger = logging.getLogger("scaffold")

# A snippet is a line or two; a page worth keeping says more than that.
_MIN_BODY_CHARS = 200
_BODY_CAP = 2000
# Pages recalled into one pass, newest first — they ride alongside fresh research, not instead of it.
_RECALL_LIMIT = 3


def _keepable(source: dict) -> bool:
    from app.modules.research_extractors import _is_bare_homepage
    url = str(source.get("url") or "")
    return (source.get("kind") == "web" and url.startswith("http")
            and not _is_bare_homepage(url)
            and len(str(source.get("text") or "").strip()) >= _MIN_BODY_CHARS)


async def keep_and_recall(db, *, session_id: Optional[str], node_key: Optional[str],
                          sources: list[dict]) -> list[dict]:
    """Keep this pass's web pages for the step; return `sources` plus the step's earlier pages not in it."""
    if db is None or not session_id or not node_key:
        return sources
    try:
        from sqlalchemy import text
        async with savepoint(db):
            for s in sources:
                if not _keepable(s):
                    continue
                await db.execute(text(
                    "INSERT INTO assist_step_sources "
                    "  (session_id, node_key, url, title, body, published, query) "
                    "VALUES (CAST(:sid AS uuid), :nk, :url, :title, :body, :published, :query) "
                    "ON CONFLICT (session_id, node_key, url) DO UPDATE SET "
                    "  title = EXCLUDED.title, body = EXCLUDED.body, published = EXCLUDED.published, "
                    "  query = EXCLUDED.query, found_at = NOW()"),
                    {"sid": str(session_id), "nk": node_key, "url": s["url"],
                     "title": str(s.get("title") or "")[:300],
                     "body": str(s.get("text") or "")[:_BODY_CAP],
                     "published": str(s.get("date") or "")[:10],
                     "query": str(s.get("query") or "")[:300]})
            fresh = [str(s.get("url") or "") for s in sources if s.get("url")]
            rows = (await db.execute(text(
                "SELECT url, title, body, published, query, found_at FROM assist_step_sources "
                "WHERE session_id = CAST(:sid AS uuid) AND node_key = :nk "
                "  AND NOT (url = ANY(:fresh)) "
                "ORDER BY found_at DESC LIMIT :lim"),
                {"sid": str(session_id), "nk": node_key, "fresh": fresh,
                 "lim": _RECALL_LIMIT})).mappings().all()
        kept = [{
            "kind": "web", "url": str(r["url"]), "title": str(r["title"] or ""), "text": str(r["body"]),
            "date": str(r["published"] or ""),
            "query": f"{r['query']} (kept from this step's research on {str(r['found_at'])[:10]})",
        } for r in rows]
    except Exception as exc:
        logger.warning("assist_step_sources_failed node_key=%s err=%r", node_key, exc)
        return sources
    if not kept:
        return sources
    logger.info("assist_step_sources_recalled node_key=%s fresh=%d kept=%d urls=%r",
                node_key, len(sources), len(kept), [k["url"][:100] for k in kept])
    return list(sources) + kept
