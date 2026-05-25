"""§17.298 — OpenAPI direct-research mode.

Lifted byte-for-byte from ``app/modules/research_agent.py``
(pre-§17.298 ``_run_research_openapi_mode``). Behavior is preserved;
only the file boundary moved. ``research_agent`` re-imports the public
name as ``_run_research_openapi_mode`` so callers stay unchanged.

Sharing surface:

  - ``_sse`` / ``_await_with_heartbeat`` from ``research_state`` — top-
    level imports; no circular concerns.
  - ``_ingest_and_finalize_direct`` from ``research_agent`` — late-
    bound inside ``run_research_openapi_mode`` to break the import
    cycle (research_agent imports this module too).
"""
from __future__ import annotations

import asyncio
from typing import AsyncGenerator

from app.modules.research_state import (
    ResearchState,
    _await_with_heartbeat,
    _sse,
)


async def run_research_openapi_mode(
    spec_url: str,
    state: ResearchState,
    session_id: str,
    t0: float,
) -> AsyncGenerator[str, None]:
    """OpenAPI-mode: fetch + validate spec, ingest one entry per endpoint."""
    from app.utils.openapi_ingest import fetch_and_parse_spec
    # §17.298 — late-bound to break the research_agent ↔ research_modes
    # import cycle. See research_modes/__init__.py for the rationale.
    from app.modules.research_agent import _ingest_and_finalize_direct

    state.outline_facets = ["openapi_spec"]
    state.iteration = 1
    yield _sse("decomposition_complete", {
        "complexity": "direct",
        "facets": state.outline_facets,
        "query_count": 0,
    })
    yield _sse("iteration_started", {
        "iteration": 1,
        "query_count": 0,
        "mode": "openapi",
    })

    task = asyncio.create_task(fetch_and_parse_spec(spec_url))
    async for hb in _await_with_heartbeat(
        task, {"status": "fetching_openapi", "iteration": 1},
        session_id=session_id,
    ):
        yield hb
    endpoints, meta = task.result()

    if not endpoints:
        raise RuntimeError(f"No endpoints found in spec at {spec_url}")

    yield _sse("search_complete", {
        "iteration": 1,
        "results_found": len(endpoints),
        "total_urls": 1,
        "mode": "openapi",
        "spec_title": meta["title"],
        "spec_version": meta["spec_version"],
        "openapi_version": meta["version"],
        "truncated": meta["truncated"],
    })

    state.url_history.add(spec_url)
    state.search_history.add(f"openapi:{spec_url}".lower())

    entries: list[dict] = []
    for ep in endpoints:
        source_url = f"{spec_url}#{ep['method']} {ep['path']}"
        tags = ep.get("tags") or []
        primary_facet = tags[0] if tags else "openapi_spec"
        entries.append({
            "title": ep["title"],
            "content": ep["content"],
            "source": source_url,
            "source_type": "tech_docs",
            "confidence_score": 0.95,
            "facet": primary_facet,
            "domain_tags": tags,
        })

    yield _sse("extraction_complete", {
        "iteration": 1,
        "entries_extracted": len(entries),
        "mode": "openapi",
    })

    async for evt in _ingest_and_finalize_direct(
        state=state,
        session_id=session_id,
        entries=entries,
        mode="openapi",
        topic=f"openapi:{spec_url}",
        t0=t0,
        extra_complete_fields={
            "spec_title": meta["title"],
            "spec_version": meta["spec_version"],
            "openapi_version": meta["version"],
            "endpoints_found": meta["total_endpoints"],
            "endpoints_ingested": meta["ingested_endpoints"],
            "truncated": meta["truncated"],
        },
    ):
        yield evt
