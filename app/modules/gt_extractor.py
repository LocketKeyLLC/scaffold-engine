"""Scaffold Engine -- Ground truth extractor module.

Extracts ground truths for a topic via:
  1. SearXNG web search for current facts
  2. LLM distillation into discrete knowledge entries
  3. TOON formatting with sanitization
  4. Optional push to GitHub KB via API
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import secrets
from datetime import datetime, timezone
from typing import Any

from app import model_router
from app.config import settings
from app.utils.github_ingest import (
    GitHubRateLimitError,
    GitHubRepoNotFoundError,
    check_github_rate_limit,
)
from app.providers.base import Tool
from app.utils.http_clients import get_github_client, get_searxng_client
from app.utils.tool_call_args import read_tool_args
from app.utils.topic_detection import detect_topic_id as _topic_detect_impl

logger = logging.getLogger("scaffold.gt")

# ---------------------------------------------------------------------------
# TOON topic map
# ---------------------------------------------------------------------------

TOPIC_MAP = {
    1: "llm-research",
    2: "rag-systems",
    3: "engineering-patterns",
    4: "dev-methodologies",
    5: "code-patterns",
    6: "token-optimization",
}

TOPIC_KEYWORDS = {
    1: ["llm", "transformer", "attention", "training", "inference", "prompt", "model", "fine-tun", "rlhf", "quantiz"],
    2: ["rag", "retriev", "chunk", "embed", "vector", "rerank", "milvus", "semantic search"],
    3: ["architecture", "pattern", "microservice", "api", "design pattern", "distributed", "scalab"],
    4: ["ci/cd", "pipeline", "github action", "deploy", "devops", "agile", "tdd", "docker", "test"],
    5: ["python", "javascript", "snippet", "algorithm", "data structure", "bash", "sql", "decorator"],
    6: ["toon", "token", "serializ", "format", "compression", "schema", "bpe"],
}

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

DISTILL_SYSTEM = """You are a knowledge distillation engine. Given raw search results about a topic, extract discrete factual knowledge entries.

Rules:
- Each entry is ONE atomic fact or concept
- Be specific: include numbers, names, versions where applicable
- Discard noise, opinions, marketing language
- 5-10 entries per extraction
- Content must NOT contain escaped quotes or backslashes
- If no useful facts found, return an empty entries array"""

DISTILL_PROMPT = """Extract factual knowledge entries from these search results about: {topic}

Search results:
---
{results}
---"""

# Sprint X.12 — native tool-call schema. The wrapper parses structured args
# on native-tool providers and falls back to JSON-coaxing on non-native
# providers, so callers always read entries via resp.tool_calls[0].
# arguments["entries"]. Replaces the legacy "OUTPUT FORMAT (strict JSON
# array)..." prose block in DISTILL_SYSTEM.
RECORD_DISTILLED_ENTRIES_TOOL = Tool(
    name="record_distilled_entries",
    description=(
        "Record extracted factual knowledge entries from search results."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "entries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "Short-hyphenated-title",
                        },
                        "content": {
                            "type": "string",
                            "description": (
                                "Single self-contained fact. Technically "
                                "precise. No filler."
                            ),
                        },
                        "tags": {
                            "type": "string",
                            "description": "Comma-separated tags",
                        },
                        "source": {
                            "type": "string",
                            "description": "URL or citation",
                        },
                    },
                    "required": ["title", "content"],
                },
            },
        },
        "required": ["entries"],
    },
)


# ---------------------------------------------------------------------------
# SearXNG search
# ---------------------------------------------------------------------------

async def search_searxng(query: str, max_results: int = 10) -> list[dict]:
    """Query SearXNG and return result list."""
    try:
        client = get_searxng_client()
        resp = await client.get(
            "/search",
            params={"q": query, "format": "json", "categories": "general"},
        )
        if resp.status_code != 200:
            logger.warning("SearXNG returned %d for query: %s", resp.status_code, query)
            return []

        data = resp.json()
        results = data.get("results", [])[:max_results]
        return [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "content": r.get("content", ""),
            }
            for r in results
        ]
    except Exception as e:
        logger.error("SearXNG search failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# TOON formatting
# ---------------------------------------------------------------------------

def sanitize_toon_content(text: str) -> str:
    """Escape a string for TOON quoted fields.

    Order matters: escape backslashes FIRST so subsequent \\n, \\t, \\"
    insertions aren't double-escaped on re-read.
    """
    return (
        text.replace("\\", "\\\\")
            .replace("\n", "\\n")
            .replace("\t", "\\t")
            .replace('"', '\\"')
    )


def format_toon_rows(entries: list[dict]) -> list[str]:
    """Convert knowledge entries to TOON data rows."""
    rows = []
    for i, entry in enumerate(entries):
        eid = i + 1
        title = entry.get("title", "unknown").strip().lower().replace(" ", "-")
        content = sanitize_toon_content(entry.get("content", ""))
        tags = sanitize_toon_content(
            ",".join(t.strip().lower() for t in entry.get("tags", "").split(","))
        )
        raw_source = entry.get("source", "pending-verification").strip() or "pending-verification"
        source = sanitize_toon_content(raw_source)
        rows.append(f'  {eid},{title},"{content}","{tags}","{source}",false,pending')
    return rows


def _detect_topic_id(topic_text: str) -> int:
    """Auto-detect topic category from text (delegates to shared util)."""
    return _topic_detect_impl(topic_text, TOPIC_KEYWORDS, default=1)


def _normalize_legacy_keys(entries: list[dict]) -> list[dict]:
    """Tolerate LLM drift: map legacy 'topic' → 'title' in-place."""
    for e in entries:
        if "title" not in e and "topic" in e:
            e["title"] = e.pop("topic")
    return entries


# ---------------------------------------------------------------------------
# GitHub push
# ---------------------------------------------------------------------------

# §17.292 — operator-facing categories for push_to_github failures. The
# return dict carries `category` so UIs / CLIs can dispatch retry
# behavior by kind (rate-limit → backoff + retry-after, auth → re-auth
# prompt, not_found → ask-for-path) instead of regex-parsing the
# free-text `reason` field. The string set is closed — adding a new
# category here is the only way to introduce one.
_PUSH_CATEGORY_CONFIG = "config"            # missing settings / token
_PUSH_CATEGORY_AUTH = "auth"                # 401, 403
_PUSH_CATEGORY_NOT_FOUND = "not_found"      # 404, GitHubRepoNotFoundError
_PUSH_CATEGORY_RATE_LIMIT = "rate_limit"    # 429, GitHubRateLimitError
_PUSH_CATEGORY_SERVER = "server"            # 5xx
_PUSH_CATEGORY_NETWORK = "network"          # connection / timeout
_PUSH_CATEGORY_UNKNOWN = "unknown"          # catch-all


def _push_category_for_status(status: int) -> str:
    """Map a GitHub HTTP status to a §17.292 failure category."""
    if status in (401, 403):
        return _PUSH_CATEGORY_AUTH
    if status == 404:
        return _PUSH_CATEGORY_NOT_FOUND
    if status == 429:
        return _PUSH_CATEGORY_RATE_LIMIT
    if 500 <= status < 600:
        return _PUSH_CATEGORY_SERVER
    return _PUSH_CATEGORY_UNKNOWN


def _push_category_for_exception(exc: Exception) -> str:
    """Map an unexpected exception to a §17.292 failure category.

    Heuristic — checks the exception class name so we don't have to
    import every httpx/socket type up here. Network-class errors share
    the ``Connect`` / ``Timeout`` substrings; everything else is
    ``unknown`` (preserves the pre-§17.292 catch-all behaviour while
    still giving the UI a stable dispatch token).
    """
    name = type(exc).__name__
    if "Timeout" in name or "Connect" in name or "Network" in name:
        return _PUSH_CATEGORY_NETWORK
    return _PUSH_CATEGORY_UNKNOWN


def _push_failure(
    category: str, detail: str, *, reason: str | None = None,
) -> dict:
    """Build the §17.292 standard push-failure dict.

    ``reason`` is kept for backward compatibility with consumers that
    haven't migrated to ``category``/``detail`` yet — defaults to
    ``"{category}: {detail}"`` so the legacy string stays readable.
    """
    return {
        "pushed": False,
        "category": category,
        "detail": detail,
        "reason": reason if reason is not None else f"{category}: {detail}",
    }


async def push_to_github(
    rows: list[str],
    file_path: str,
    topic: str,
    *,
    owner: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
) -> dict:
    """Push TOON rows to GitHub via Contents API. Returns result dict.

    Kwargs default to settings.gt_github_{owner,repo,branch}, keeping the
    positional signature backward-compatible with existing callers.
    """
    if not settings.github_token:
        return _push_failure(
            _PUSH_CATEGORY_CONFIG,
            "github_token not set in settings",
            reason="github_token not set in settings",
        )

    owner = owner or settings.gt_github_owner
    repo = repo or settings.gt_github_repo
    branch = branch or settings.gt_github_branch

    client = get_github_client()
    repo_base = f"/repos/{owner}/{repo}"

    try:
        # 0. Rate-limit preflight (cheap, surfaces 403 early)
        rate_resp = await client.get("/rate_limit")
        check_github_rate_limit(rate_resp)

        # 1. Get current file (or 404 → fresh header)
        resp = await client.get(
            f"{repo_base}/contents/{file_path}",
            params={"ref": branch},
        )
        check_github_rate_limit(resp)

        if resp.status_code == 200:
            existing = base64.b64decode(resp.json()["content"]).decode("utf-8")
        elif resp.status_code == 404:
            existing = _new_toon_header(file_path)
        else:
            # §17.292 — note: 404 on this read path is success
            # (new-file case, handled above). Other 4xx/5xx flow here.
            return _push_failure(
                _push_category_for_status(resp.status_code),
                f"GitHub GET failed: {resp.status_code}",
                reason=f"GitHub GET failed: {resp.status_code}",
            )

        # 2. Append new rows
        updated = existing
        for row in rows:
            updated = _append_toon_row(updated, row)

        # 3. Get base-branch SHA
        ref_resp = await client.get(f"{repo_base}/git/ref/heads/{branch}")
        check_github_rate_limit(ref_resp)
        ref_resp.raise_for_status()
        main_sha = ref_resp.json()["object"]["sha"]

        # 4. Create feature branch (microsecond + random suffix to avoid collisions)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        rand = secrets.token_hex(2)  # 4 hex chars
        safe_topic = re.sub(r"[^a-z0-9-]", "", topic.lower().replace(" ", "-"))[:30]
        new_branch = f"knowledge/{safe_topic}-{ts}-{rand}"

        create_resp = await client.post(
            f"{repo_base}/git/refs",
            json={"ref": f"refs/heads/{new_branch}", "sha": main_sha},
        )
        check_github_rate_limit(create_resp)
        if create_resp.status_code not in (200, 201):
            return _push_failure(
                _push_category_for_status(create_resp.status_code),
                f"Branch creation failed: {create_resp.status_code}",
                reason=f"Branch creation failed: {create_resp.status_code}",
            )

        # 5. Get file SHA on new branch (if present)
        branch_resp = await client.get(
            f"{repo_base}/contents/{file_path}",
            params={"ref": new_branch},
        )
        check_github_rate_limit(branch_resp)
        branch_file_sha = None
        if branch_resp.status_code == 200:
            branch_file_sha = branch_resp.json()["sha"]

        # 6. Push updated file
        payload: dict[str, Any] = {
            "message": f"knowledge: add {len(rows)} entries for '{topic}' via Scaffold Engine",
            "content": base64.b64encode(updated.encode("utf-8")).decode("utf-8"),
            "branch": new_branch,
        }
        if branch_file_sha:
            payload["sha"] = branch_file_sha

        put_resp = await client.put(
            f"{repo_base}/contents/{file_path}",
            json=payload,
        )
        check_github_rate_limit(put_resp)
        if put_resp.status_code not in (200, 201):
            return _push_failure(
                _push_category_for_status(put_resp.status_code),
                f"Push failed: {put_resp.status_code}",
                reason=f"Push failed: {put_resp.status_code}",
            )

        # 7. Open PR
        pr_resp = await client.post(
            f"{repo_base}/pulls",
            json={
                "title": f"knowledge: add {topic} entries via Scaffold Engine",
                "head": new_branch,
                "base": branch,
                "body": f"Add {len(rows)} TOON entries for `{topic}`.\n\nPushed via Scaffold Engine GT extractor.",
            },
        )
        check_github_rate_limit(pr_resp)
        pr_data = pr_resp.json()

        return {
            "pushed": True,
            "owner": owner,
            "repo": repo,
            "branch": new_branch,
            "pr_number": pr_data.get("number"),
            "pr_url": pr_data.get("html_url", ""),
        }

    except GitHubRateLimitError as e:
        logger.warning("GitHub rate limit exhausted: %s", e)
        # §17.292 — preserve the pre-existing `"rate_limit: ..."` reason
        # string for backward compatibility while adding the structured
        # `category` field. Consumers reading either field continue to
        # work; new consumers should dispatch off `category`.
        return _push_failure(
            _PUSH_CATEGORY_RATE_LIMIT, str(e), reason=f"rate_limit: {e}",
        )
    except GitHubRepoNotFoundError as e:
        logger.warning("GitHub repo not found: %s", e)
        return _push_failure(
            _PUSH_CATEGORY_NOT_FOUND, str(e), reason=f"not_found: {e}",
        )
    except Exception as e:
        logger.error("GitHub push failed: %s", e, exc_info=True)
        return _push_failure(
            _push_category_for_exception(e), str(e), reason=str(e),
        )


# ---------------------------------------------------------------------------
# TOON file helpers
# ---------------------------------------------------------------------------

def _new_toon_header(file_path: str) -> str:
    category = file_path.split("/")[-1].replace(".toon", "")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"meta:\n"
        f"  schema_v: 1\n"
        f"  source: https://github.com/{settings.gt_github_owner}/{settings.gt_github_repo}\n"
        f"  timestamp: {ts}\n"
        f"  content_type: technical-knowledge\n"
        f"  category: {category}\n\n"
        f"knowledge[0]{{id,title,content,tags,source,verified,last_verified}}:\n"
    )


def _append_toon_row(file_content: str, row: str) -> str:
    clean_row = row.strip()
    if not clean_row.startswith("  "):
        clean_row = f"  {clean_row}"

    pattern = r"(knowledge\[)(\d+)(\]\{[^}]+\}:)"
    match = re.search(pattern, file_content)

    if match is None:
        raise ValueError(
            "TOON header missing or malformed: expected 'knowledge[N]{...}:' block"
        )
    current_count = int(match.group(2))
    new_count = current_count + 1

    # §17.600 — always renumber the appended row's leading id to the new header
    # count. format_toon_rows emits literal ids restarting at 1 (for display /
    # fresh-file blocks), and the old code only renumbered rows prefixed
    # "AUTO," — a sentinel that's never emitted — so every append to a
    # non-empty file collided ids. Rewrite the leading id (digits or the legacy
    # AUTO sentinel) in place.
    clean_row = re.sub(
        r"^(\s*)(?:\d+|AUTO)(,)",
        rf"\g<1>{new_count}\g<2>",
        clean_row,
        count=1,
    )
    if not clean_row.startswith("  "):
        clean_row = f"  {clean_row}"

    file_content = re.sub(pattern, f"\\g<1>{new_count}\\g<3>", file_content)

    if file_content.endswith("\n"):
        file_content += clean_row + "\n"
    else:
        file_content += "\n" + clean_row + "\n"

    return file_content


# ---------------------------------------------------------------------------
# Shared distill primitive
# ---------------------------------------------------------------------------

async def distill_entries(
    results: list[dict],
    *,
    topic: str,
    route: dict | None = None,
    max_results: int = 15,
) -> list[dict]:
    """Distill raw SearXNG results into structured knowledge entries.

    The single reliable distill path, shared by :func:`extract_ground_truths`
    and the ideation Phase-2 grounded-research step. Uses the native
    ``model_router.tool_call`` + ``RECORD_DISTILLED_ENTRIES_TOOL`` contract so
    the model is forced to emit objects (``{title, content, tags?, source?}``).

    §17.x — replaces the legacy ``generate`` + ``parse_json_array`` path that
    silently dropped 100% of results: ``DISTILL_SYSTEM`` no longer carries an
    object-shape spec (it moved into the tool schema), so the bare-generate
    path let the 4b model return an array of *strings* which the dict-filter
    discarded (``phase2_distill_shape_drift: raw=10 kept=0 dropped=10``).

    Returns ``[]`` on any soft failure (no results, LLM error, parse failure,
    empty draw) — research grounding is best-effort and must never raise into
    the ideation/decompose flow.
    """
    if not results:
        return []
    results_text = "\n\n".join(
        f"Title: {r.get('title', '')}\nURL: {r.get('url', '')}\n"
        f"Snippet: {r.get('content', '')}"
        for r in results[:max_results]
    )
    route = route or {"role": "model_router"}
    resp = await model_router.tool_call(
        messages=[
            {"role": "system", "content": DISTILL_SYSTEM},
            {"role": "user",
             "content": DISTILL_PROMPT.format(topic=topic, results=results_text)},
        ],
        tools=[RECORD_DISTILLED_ENTRIES_TOOL],
        temperature=0.2,
        max_tokens=4096,
        **route,
    )
    if not resp.success:
        logger.warning("distill_entries: llm_failed topic=%r err=%s", topic, resp.error)
        return []
    args = read_tool_args(resp)
    if args is None or not isinstance(args.get("entries"), list):
        logger.warning(
            "distill_entries: parse_failed topic=%r raw=%r",
            topic, (resp.text or "")[:200],
        )
        return []
    entries = [e for e in args["entries"] if isinstance(e, dict)]
    if not entries:
        return []
    return _normalize_legacy_keys(entries)


async def quick_research(
    queries: list[str],
    *,
    domain: str = "eng",
    top_k: int = 15,
    ingest: bool = False,
    route: dict | None = None,
) -> dict:
    """Fast, grounded standards research: SearXNG search → distill, no loop.

    §17.x — the synchronous counterpart to the autonomous ``/research`` SSE
    loop (which runs 20-60 min). Reuses :func:`search_searxng` +
    :func:`distill_entries` so the result is grounded in real sources, not the
    triage model's memory. Used batched-at-/go by the decomposition fan-out
    (one call per component) and exposed via ``POST /research/quick``.

    Returns ``{entries, results_found, ingested}``; ``entries`` is ``[]`` on any
    soft failure (research is best-effort and must never raise into /go).
    """
    if not queries:
        return {"entries": [], "results_found": 0, "ingested": 0}

    all_results: list[dict] = []
    seen_urls: set[str] = set()
    for i, q in enumerate(queries[: settings.ideation_max_queries]):
        if i > 0:
            await asyncio.sleep(settings.research_searxng_delay)
        for r in await search_searxng(q):
            url = r.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                all_results.append(r)

    entries = await distill_entries(
        all_results, topic=queries[0], route=route, max_results=top_k,
    )

    ingested = 0
    if ingest and entries:
        from app.modules.rag_pipeline import ingest_entries
        stats = await ingest_entries(entries, domain=domain or "eng")
        ingested = stats["new"] + stats["versioned"]

    return {
        "entries": entries,
        "results_found": len(all_results),
        "ingested": ingested,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def extract_ground_truths(
    topic: str,
    *,
    queries: list[str] | None = None,
    push_to_github: bool = False,
    target_file: str | None = None,
    model: str | None = None,
    github_owner: str | None = None,
    github_repo: str | None = None,
) -> dict:
    """Extract ground truths for a topic via SearXNG + LLM distillation."""
    if not queries:
        queries = [topic, f"{topic} best practices", f"{topic} technical details"]

    all_results: list[dict] = []
    for i, query in enumerate(queries[:5]):
        # Sleep between calls (mirror research_agent.py rate limiting). The
        # first call doesn't need to wait — only inter-call gaps matter for
        # not starving a shared SearXNG instance.
        if i > 0:
            await asyncio.sleep(settings.research_searxng_delay)
        results = await search_searxng(query)
        all_results.extend(results)
        logger.info("SearXNG: %d results for '%s'", len(results), query)

    if not all_results:
        return {
            "status": "no_results",
            "topic": topic,
            "error": "SearXNG returned no results for any query",
        }

    # Dedupe by URL
    seen_urls: set[str] = set()
    unique_results: list[dict] = []
    for r in all_results:
        url = r.get("url", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            unique_results.append(r)

    # Distill via LLM (model_router / 4b — snippet-level distillation)
    results_text = "\n\n".join(
        f"Title: {r['title']}\nURL: {r['url']}\nSnippet: {r['content']}"
        for r in unique_results[:15]
    )

    prompt = DISTILL_PROMPT.format(topic=topic, results=results_text)
    route_kwargs = {"model": model} if model else {"role": "model_router"}
    resp = await model_router.tool_call(
        messages=[
            {"role": "system", "content": DISTILL_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        tools=[RECORD_DISTILLED_ENTRIES_TOOL],
        temperature=0.2,
        max_tokens=4096,
        **route_kwargs,
    )

    if not resp.success:
        return {"status": "llm_failed", "topic": topic, "error": resp.error}

    # Sprint X.12 — read structured args. Wrapper handles parsing on both
    # native and coaxing providers. Failure path (no tool_calls, missing
    # 'entries' key, wrong type) maps to status='parse_failed' to preserve
    # the pre-X.12 caller contract.
    args = read_tool_args(resp)
    if args is None or not isinstance(args.get("entries"), list):
        return {
            "status": "parse_failed",
            "topic": topic,
            "error": "LLM did not produce a valid entries array",
            "raw_output": (resp.text or "")[:500],
        }
    entries = args["entries"]

    if not entries:
        return {
            "status": "empty",
            "topic": topic,
            "error": "LLM returned an empty entry array",
        }

    entries = _normalize_legacy_keys(entries)
    toon_rows = format_toon_rows(entries)

    if not target_file:
        topic_id = _detect_topic_id(topic)
        target_file = f"knowledge/{TOPIC_MAP.get(topic_id, 'llm-research')}.toon"

    result: dict[str, Any] = {
        "status": "extracted",
        "topic": topic,
        "entry_count": len(entries),
        "entries": entries,
        "toon_rows": toon_rows,
        "target_file": target_file,
        "search_results_used": len(unique_results),
        "model_used": resp.model,
        "duration_ms": resp.total_duration_ms,
    }

    if push_to_github:
        # The `push_to_github: bool` parameter shadows the module-level
        # `async def push_to_github`; call the module alias so `await` targets
        # the coroutine, not the boolean flag. (§17.594)
        gh_result = await _push_to_github(
            toon_rows,
            target_file,
            topic,
            owner=github_owner,
            repo=github_repo,
        )
        result["github"] = gh_result

    return result


# Sprint X.12 — `_parse_entries` and `_ParseFailed` removed. The
# `model_router.tool_call` wrapper handles JSON-array parsing on both
# native-tool and coaxing-fallback providers; failures surface via
# `read_tool_args` returning None (or args["entries"] not being a list).


# --- backward-compat aliases ---
_search_searxng = search_searxng
_format_toon_rows = format_toon_rows
_push_to_github = push_to_github
