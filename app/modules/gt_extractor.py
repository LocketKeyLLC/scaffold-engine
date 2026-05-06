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
from app.utils.http_clients import get_github_client, get_searxng_client
from app.utils.llm_parsing import parse_json_array
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

OUTPUT FORMAT (strict JSON array, no markdown fences):
[
  {
    "title": "short-hyphenated-title",
    "content": "Single self-contained fact. Technically precise. No filler.",
    "tags": "comma,separated,tags",
    "source": "URL or citation"
  }
]

Rules:
- Each entry is ONE atomic fact or concept
- Be specific: include numbers, names, versions where applicable
- Discard noise, opinions, marketing language
- 5-10 entries per extraction
- Content must NOT contain escaped quotes or backslashes
- The "title" field is REQUIRED on every entry
- If no useful facts found, return an empty array []"""

DISTILL_PROMPT = """Extract factual knowledge entries from these search results about: {topic}

Search results:
---
{results}
---

Return ONLY the JSON array."""


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
        return {"pushed": False, "reason": "github_token not set in settings"}

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
            return {"pushed": False, "reason": f"GitHub GET failed: {resp.status_code}"}

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
            return {"pushed": False, "reason": f"Branch creation failed: {create_resp.status_code}"}

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
            return {"pushed": False, "reason": f"Push failed: {put_resp.status_code}"}

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
        return {"pushed": False, "reason": f"rate_limit: {e}"}
    except GitHubRepoNotFoundError as e:
        logger.warning("GitHub repo not found: %s", e)
        return {"pushed": False, "reason": f"not_found: {e}"}
    except Exception as e:
        logger.error("GitHub push failed: %s", e, exc_info=True)
        return {"pushed": False, "reason": str(e)}


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

    if clean_row.strip().startswith("AUTO,"):
        clean_row = clean_row.replace("AUTO,", f"{new_count},", 1)
        if not clean_row.startswith("  "):
            clean_row = f"  {clean_row}"

    file_content = re.sub(pattern, f"\\g<1>{new_count}\\g<3>", file_content)

    if file_content.endswith("\n"):
        file_content += clean_row + "\n"
    else:
        file_content += "\n" + clean_row + "\n"

    return file_content


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
    resp = await model_router.generate(
        prompt,
        model=model or settings.model_router,
        system=DISTILL_SYSTEM,
        temperature=0.2,
        max_tokens=4096,
    )

    if not resp.success:
        return {"status": "llm_failed", "topic": topic, "error": resp.error}

    try:
        entries = _parse_entries(resp.text)
    except _ParseFailed as exc:
        return {
            "status": "parse_failed",
            "topic": topic,
            "error": f"Could not parse LLM output as JSON array: {exc}",
            "raw_output": resp.text[:500],
        }

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
        gh_result = await push_to_github(
            toon_rows,
            target_file,
            topic,
            owner=github_owner,
            repo=github_repo,
        )
        result["github"] = gh_result

    return result


class _ParseFailed(Exception):
    """LLM output could not be parsed as a JSON array."""


def _parse_entries(raw: str) -> list[dict]:
    """Parse JSON array from LLM output.

    Returns:
        list[dict]: parsed entries (possibly empty if the LLM returned `[]`).

    Raises:
        _ParseFailed: the output was not a valid JSON array.
    """
    parsed = parse_json_array(raw)
    if parsed is None:
        raise _ParseFailed("parse_json_array returned None")
    return parsed


# --- backward-compat aliases ---
_search_searxng = search_searxng
_format_toon_rows = format_toon_rows
_push_to_github = push_to_github
