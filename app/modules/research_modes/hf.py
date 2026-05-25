"""§17.298 — Hugging Face direct-research mode.

Lifted byte-for-byte from ``app/modules/research_agent.py``
(pre-§17.298 ``_run_research_hf_mode``). See research_modes/__init__.py
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


async def run_research_hf_mode(
    kind: str,
    id_: str,
    state: ResearchState,
    session_id: str,
    t0: float,
) -> AsyncGenerator[str, None]:
    """HF-mode: fetch model_card / dataset_card / paper_abstract / space
    metadata, ingest with §17.104 provenance pinned to HF revision SHA.
    """
    from app.utils.hf_ingest import fetch_hf, HFNotFoundError, HFRateLimitError
    from app.modules.provenance import build_provenance
    from app.utils.fetch_cache import get_fetch_cache
    from app.utils.markdown_chunker import split_markdown_by_kind
    # §17.298 — late-bound to break the research_agent ↔ research_modes
    # import cycle. See research_modes/__init__.py for the rationale.
    from app.modules.research_agent import _ingest_and_finalize_direct

    state.outline_facets = [f"hf_{kind}"]
    state.iteration = 1
    yield _sse("decomposition_complete", {
        "complexity": "direct",
        "facets": state.outline_facets,
        "query_count": 0,
    })
    yield _sse("iteration_started", {
        "iteration": 1,
        "query_count": 0,
        "mode": "hf",
        "hf_kind": kind,
    })

    _cache_pre = get_fetch_cache().stats().copy()

    task = asyncio.create_task(fetch_hf(kind, id_))
    async for hb in _await_with_heartbeat(
        task, {"status": "fetching_hf", "iteration": 1},
        session_id=session_id,
    ):
        yield hb
    items = task.result()

    # §17.117 — cache delta across all HF API + raw-file fetches.
    _cache_post = get_fetch_cache().stats()
    _cache_delta = {k: _cache_post[k] - _cache_pre[k] for k in _cache_post}
    if _cache_delta["hits"] > 0 or _cache_delta["misses"] > 0:
        yield _sse("cache_hit_upstream", {
            "iteration": 1,
            "mode": "hf",
            "hf_kind": kind,
            **_cache_delta,
        })

    # §17.110 — emit resolved revision SHA / arXiv id for UI display.
    if items:
        yield _sse("source_ref_resolved", {
            "iteration": 1,
            "mode": "hf",
            "hf_kind": kind,
            "resolved_ref": items[0].get("source_ref", ""),
        })

    if not items:
        raise RuntimeError(f"No ingestible content found at hf:{kind}/{id_}")

    yield _sse("search_complete", {
        "iteration": 1,
        "results_found": len(items),
        "total_urls": len(items),
        "mode": "hf",
        "hf_kind": kind,
    })

    # §17.119 — split markdown bodies (model/dataset/space READMEs) on
    # fenced code blocks. Structured "metadata" entries pass through unsplit.
    _HF_MARKDOWN_SPLIT_TYPES = {"model_card", "dataset_card", "tech_docs"}
    entries: list[dict] = []
    for it in items:
        source_url = it.get("source_url", "")
        if source_url:
            state.url_history.add(source_url)
        item_source_type = it.get("source_type", "tech_docs")
        body = it["content"]
        if item_source_type in _HF_MARKDOWN_SPLIT_TYPES:
            parts = split_markdown_by_kind(body)
        else:
            parts = [(body, "prose")]
        for i, (chunk_text, chunk_kind) in enumerate(parts):
            suffix = f"#{chunk_kind}-{i}" if len(parts) > 1 else ""
            entries.append({
                "title": f"hf:{kind}/{id_}: {it['path']}{suffix}",
                "content": chunk_text,
                "source": source_url,
                "source_type": item_source_type,
                "facet": f"hf_{kind}",
                "domain_tags": [f"hf_{kind}", chunk_kind],
                "provenance": build_provenance(
                    source_ref=it.get("source_ref", ""),
                    quality_signal=it.get("quality_signal", {}),
                ),
            })

    yield _sse("extraction_complete", {
        "iteration": 1,
        "entries_extracted": len(entries),
        "mode": "hf",
    })

    async for evt in _ingest_and_finalize_direct(
        state=state,
        session_id=session_id,
        entries=entries,
        mode="hf",
        topic=f"hf:{kind}/{id_}",
        t0=t0,
        extra_complete_fields={"hf_kind": kind, "items_fetched": len(items)},
    ):
        yield evt
