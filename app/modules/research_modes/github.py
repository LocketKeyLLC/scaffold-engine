"""§17.298 — GitHub direct-research mode.

Lifted byte-for-byte from ``app/modules/research_agent.py``
(pre-§17.298 ``_run_research_github_mode``). See research_modes/__init__.py
for the extraction rationale + late-import pattern.
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncGenerator

from app.modules.research_state import (
    ResearchState,
    _await_with_heartbeat,
    _sse,
)

logger = logging.getLogger("scaffold.research.modes.github")


async def run_research_github_mode(
    owner: str,
    repo: str,
    state: ResearchState,
    session_id: str,
    t0: float,
    ref_hint: str | None = None,
) -> AsyncGenerator[str, None]:
    """GitHub-mode: deep fetch of repo content + release notes + issues/PRs.

    ``ref_hint=None`` → default branch (back-compat). Any other value
    (tag, branch, SHA) resolves to a commit SHA, locking every entry's
    provenance to that immutable ref.
    """
    from app.utils.github_ingest import (
        fetch_repo_content,
        fetch_repo_discussions,
        fetch_repo_issues_and_prs,
        fetch_repo_releases,
    )
    from app.modules.provenance import build_provenance
    from app.config import settings as _settings
    from app.utils.fetch_cache import get_fetch_cache
    from app.utils.markdown_chunker import split_markdown_by_kind
    # §17.298 — late-bound to break the research_agent ↔ research_modes
    # import cycle. See research_modes/__init__.py for the rationale.
    from app.modules.research_agent import _ingest_and_finalize_direct

    state.outline_facets = ["github_repo"]
    state.iteration = 1
    yield _sse("decomposition_complete", {
        "complexity": "direct",
        "facets": state.outline_facets,
        "query_count": 0,
    })
    yield _sse("iteration_started", {
        "iteration": 1,
        "query_count": 0,
        "mode": "github",
        "ref_hint": ref_hint,
    })

    # §17.117 — snapshot fetch_cache counters around all 3 GH fetches so the
    # runner can emit a single cache_hit_upstream event at the end with the
    # delta. The cache singleton's counters are process-global; the
    # single-running-research-per-host invariant keeps them session-scoped
    # in practice.
    _cache_pre = get_fetch_cache().stats().copy()

    task = asyncio.create_task(fetch_repo_content(owner, repo, ref_hint=ref_hint))
    async for hb in _await_with_heartbeat(
        task, {"status": "fetching_github", "iteration": 1},
        session_id=session_id,
    ):
        yield hb
    files = task.result()

    # §17.110 — surface the resolved ref so the UI can show "v1.2.3 → abc123def…".
    # For ref_hint=None this is the default-branch name (weakly immutable).
    if files:
        yield _sse("source_ref_resolved", {
            "iteration": 1,
            "mode": "github",
            "ref_hint": ref_hint,
            "resolved_ref": files[0].get("source_ref", ""),
        })

    # Release notes + issues/PRs run after the main tree walk so a tree
    # failure doesn't mask them, and so an empty tree result can still
    # surface them as content.
    releases: list[dict] = []
    issues: list[dict] = []
    discussions: list[dict] = []
    try:
        releases = await fetch_repo_releases(owner, repo, _settings.github_max_releases)
    except Exception as exc:
        logger.warning("github_releases_fetch_failed: %s/%s err=%s", owner, repo, exc)
    try:
        issues = await fetch_repo_issues_and_prs(
            owner, repo,
            _settings.github_max_issues,
            _settings.github_min_issue_reactions,
        )
    except Exception as exc:
        logger.warning("github_issues_fetch_failed: %s/%s err=%s", owner, repo, exc)
    try:
        discussions = await fetch_repo_discussions(
            owner, repo, _settings.github_max_discussions,
        )
    except Exception as exc:
        logger.warning("github_discussions_fetch_failed: %s/%s err=%s", owner, repo, exc)

    all_items = list(files) + releases + issues + discussions
    if not all_items:
        raise RuntimeError(f"No ingestible content found in {owner}/{repo}")

    # §17.117 — emit aggregate cache delta across the 3 GH fetches.
    _cache_post = get_fetch_cache().stats()
    _cache_delta = {k: _cache_post[k] - _cache_pre[k] for k in _cache_post}
    if _cache_delta["hits"] > 0 or _cache_delta["misses"] > 0:
        yield _sse("cache_hit_upstream", {
            "iteration": 1,
            "mode": "github",
            **_cache_delta,
        })

    yield _sse("search_complete", {
        "iteration": 1,
        "results_found": len(all_items),
        "total_urls": len(all_items),
        "mode": "github",
        "files": len(files),
        "releases": len(releases),
        "issues": len(issues),
        "discussions": len(discussions),
    })

    # §17.119 — for markdown-y source_types, split each item's body into
    # (chunk, kind) tuples on triple-backtick fences. One file can yield
    # multiple Milvus entries — each tagged ``kind`` in domain_tags so
    # query-intent="code" can filter on it.
    _MARKDOWN_SPLIT_SOURCE_TYPES = {"tech_docs", "release_notes", "community"}
    entries: list[dict] = []
    for f in all_items:
        source_url = f.get("source_url", "")
        if source_url:
            state.url_history.add(source_url)
        source_type = f.get("source_type", "tech_docs")
        body = f["content"]
        if source_type in _MARKDOWN_SPLIT_SOURCE_TYPES:
            parts = split_markdown_by_kind(body)
        else:
            parts = [(body, "prose")]
        for i, (chunk_text, chunk_kind) in enumerate(parts):
            suffix = f"#{chunk_kind}-{i}" if len(parts) > 1 else ""
            entries.append({
                "title": f"{owner}/{repo}: {f['path']}{suffix}",
                "content": chunk_text,
                "source": source_url,
                "source_type": source_type,
                # No confidence_score key → §17.104 derives from source_type.
                "facet": "github_repo",
                "domain_tags": ["github_repo", chunk_kind],
                "provenance": build_provenance(
                    source_ref=f.get("source_ref", ""),
                    quality_signal=f.get("quality_signal", {}),
                ),
            })

    yield _sse("extraction_complete", {
        "iteration": 1,
        "entries_extracted": len(entries),
        "mode": "github",
    })

    # Note: github mode does no LLM extraction (entries are pulled
    # directly from the GitHub API as README + docs/*.md + top-level
    # docstrings), so there's no extract model to unload here. Audit
    # Finding C only applies to modes whose extract phase loads a
    # ~5 GB model that must be freed before the embedder cold-loads.

    async for evt in _ingest_and_finalize_direct(
        state=state,
        session_id=session_id,
        entries=entries,
        mode="github",
        topic=f"github:{owner}/{repo}",
        t0=t0,
        extra_complete_fields={"files_fetched": len(files)},
    ):
        yield evt
