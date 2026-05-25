"""§17.298 — Forum direct-research mode (SO / HN / arXiv / Reddit / Wiki).

Lifted byte-for-byte from ``app/modules/research_agent.py``
(pre-§17.298 ``_run_research_forum_mode``). See research_modes/__init__.py
for the extraction rationale + late-import pattern.
"""
from __future__ import annotations

import asyncio
from typing import AsyncGenerator

from app.modules.research_state import (
    ResearchState,
    _await_with_heartbeat,
    _sse,
)


async def run_research_forum_mode(
    prefix: str,
    value: str,
    state: ResearchState,
    session_id: str,
    t0: float,
) -> AsyncGenerator[str, None]:
    """Forum-mode: SO / HN / arXiv. ``value`` is the post-prefix query.

    For arXiv we pack ``<mode>:<value>`` (e.g., ``id:2310.06825`` or
    ``query:transformer architecture``) into the ``value`` arg — the
    runner unpacks before dispatch. This avoids leaking a 3-arg dispatch
    through ResearchState.
    """
    from app.utils.forum_ingest import (
        fetch_arxiv,
        fetch_hn_items,
        fetch_reddit_posts,
        fetch_so_answers,
        fetch_wiki_pages,
    )
    from app.modules.provenance import build_provenance
    from app.config import settings as _settings
    from app.utils.fetch_cache import get_fetch_cache
    # §17.298 — late-bound to break the research_agent ↔ research_modes
    # import cycle. See research_modes/__init__.py for the rationale.
    from app.modules.research_agent import _ingest_and_finalize_direct

    state.outline_facets = [f"forum_{prefix}"]
    state.iteration = 1
    yield _sse("decomposition_complete", {
        "complexity": "direct",
        "facets": state.outline_facets,
        "query_count": 0,
    })
    yield _sse("iteration_started", {
        "iteration": 1,
        "query_count": 0,
        "mode": prefix,
    })

    # §17.110 — stats dict is populated by the gated fetchers (SO/HN/Reddit)
    # with fetched/kept/filtered_* counts. After fetch completes, emit a
    # `quality_gate_filtered` SSE so the UI can show "20 of 50 passed gates".
    fetch_stats: dict[str, int] = {}

    async def _do_fetch():
        if prefix == "so":
            return await fetch_so_answers(
                value, _settings.so_max_answers, _settings.so_min_score,
                stats=fetch_stats,
                include_disputed=_settings.forum_ingest_disputed,
            )
        if prefix == "hn":
            return await fetch_hn_items(
                value, _settings.hn_max_items, _settings.hn_min_points,
                stats=fetch_stats,
            )
        if prefix == "arxiv":
            mode, val = value.split(":", 1)
            return await fetch_arxiv(mode, val, _settings.arxiv_max_sections)
        if prefix == "reddit":
            sub, q = value.split(":", 1)
            return await fetch_reddit_posts(
                sub, q, _settings.reddit_max_posts,
                _settings.reddit_min_score, _settings.reddit_min_comments,
                stats=fetch_stats,
                include_disputed=_settings.forum_ingest_disputed,
            )
        if prefix == "wiki":
            return await fetch_wiki_pages(value, _settings.wiki_max_pages)
        raise ValueError(f"Unknown forum prefix: {prefix!r}")

    _cache_pre = get_fetch_cache().stats().copy()

    task = asyncio.create_task(_do_fetch())
    async for hb in _await_with_heartbeat(
        task, {"status": f"fetching_{prefix}", "iteration": 1},
        session_id=session_id,
    ):
        yield hb
    items = task.result()

    # §17.117 — cache delta across the forum fetch.
    _cache_post = get_fetch_cache().stats()
    _cache_delta = {k: _cache_post[k] - _cache_pre[k] for k in _cache_post}
    if _cache_delta["hits"] > 0 or _cache_delta["misses"] > 0:
        yield _sse("cache_hit_upstream", {
            "iteration": 1,
            "mode": prefix,
            **_cache_delta,
        })

    # Emit gate stats before checking emptiness so the UI sees the "why".
    if fetch_stats:
        yield _sse("quality_gate_filtered", {
            "iteration": 1,
            "mode": prefix,
            **fetch_stats,
        })

    if not items:
        raise RuntimeError(
            f"No ingestible content found for {prefix}:{value!r} "
            f"(quality gates may have filtered everything)"
        )

    yield _sse("search_complete", {
        "iteration": 1,
        "results_found": len(items),
        "total_urls": len(items),
        "mode": prefix,
    })

    entries: list[dict] = []
    for it in items:
        source_url = it.get("source_url", "")
        if source_url:
            state.url_history.add(source_url)
        entry = {
            "title": f"{prefix}: {it['path']}",
            "content": it["content"],
            "source": source_url,
            "source_type": it.get("source_type", "tech_docs"),
            "facet": f"forum_{prefix}",
            "provenance": build_provenance(
                source_ref=it.get("source_ref", ""),
                quality_signal=it.get("quality_signal", {}),
            ),
        }
        # §17.126 — pass through raw_upstream_hash when the producer
        # populated it (currently arxiv id-mode does).
        if it.get("raw_upstream_hash"):
            entry["raw_upstream_hash"] = it["raw_upstream_hash"]
        entries.append(entry)

    yield _sse("extraction_complete", {
        "iteration": 1,
        "entries_extracted": len(entries),
        "mode": prefix,
    })

    async for evt in _ingest_and_finalize_direct(
        state=state,
        session_id=session_id,
        entries=entries,
        mode=prefix,
        topic=f"{prefix}:{value}",
        t0=t0,
        extra_complete_fields={"items_fetched": len(items)},
    ):
        yield evt
