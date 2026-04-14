"""Scaffold Engine -- Ground truth extractor module.

Extracts ground truths for a topic via:
  1. SearXNG web search for current facts
  2. LLM distillation into discrete knowledge entries
  3. TOON formatting with sanitization
  4. Optional push to GitHub KB via API

Reuses patterns from build_knowledge.py and toon_knowledge_builder.py.

Step 12 of 23-step build plan.
"""

from __future__ import annotations

import json
import logging
import os
import re
import base64
from app.utils.llm_parsing import parse_json_array
from datetime import datetime, timezone
from typing import Any

import httpx

from app import model_router
from app.config import settings

logger = logging.getLogger("scaffold.gt")

# ---------------------------------------------------------------------------
# TOON topic map (from build_knowledge.py)
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
    5: ["python", "javascript", "code", "snippet", "algorithm", "data structure", "bash", "sql", "decorator"],
    6: ["toon", "token", "serializ", "format", "compression", "schema", "bpe"],
}

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

DISTILL_SYSTEM = """You are a knowledge distillation engine. Given raw search results about a topic, extract discrete factual knowledge entries.

OUTPUT FORMAT (strict JSON array, no markdown fences):
[
  {
    "topic": "short-hyphenated-topic",
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

async def _search_searxng(query: str, max_results: int = 10) -> list[dict]:
    """Query SearXNG and return result list."""
    try:
        from app.utils.http_clients import get_searxng_client
        client = get_searxng_client()
        resp = await client.get(
            "/search",
            params={
                "q": query,
                "format": "json",
                "categories": "general",
            },
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
# TOON formatting (from toon_knowledge_builder.py)
# ---------------------------------------------------------------------------

def sanitize_toon_content(text: str) -> str:
    """Sanitize text for TOON quoted fields."""
    sanitized = text.replace('\\"', "")
    sanitized = sanitized.replace('"', '""')
    return sanitized


def _format_toon_rows(entries: list[dict]) -> list[str]:
    """Convert knowledge entries to TOON data rows."""
    rows = []
    for i, entry in enumerate(entries):
        eid = i + 1
        topic = entry.get("topic", "unknown").strip().lower().replace(" ", "-")
        content = sanitize_toon_content(entry.get("content", ""))
        tags = ",".join(t.strip().lower() for t in entry.get("tags", "").split(","))
        source = entry.get("source", "pending-verification").strip() or "pending-verification"
        rows.append(f'  {eid},{topic},"{content}","{tags}",{source},false,pending')
    return rows


def _detect_topic_id(topic_text: str) -> int:
    """Auto-detect topic category from text."""
    topic_lower = topic_text.lower()
    scores = {}
    for tid, keywords in TOPIC_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in topic_lower)
        scores[tid] = score
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else 1


# ---------------------------------------------------------------------------
# GitHub push (from toon_knowledge_builder.py patterns)
# ---------------------------------------------------------------------------

async def _push_to_github(
    rows: list[str],
    file_path: str,
    topic: str,
) -> dict:
    """Push TOON rows to GitHub via Contents API. Returns result dict."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return {"pushed": False, "reason": "GITHUB_TOKEN not set"}

    owner = "LocketKeyLLC"
    repo = "smokieRAGs"
    branch = "main"
    api_base = f"https://api.github.com/repos/{owner}/{repo}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # Get current file
            resp = await client.get(
                f"{api_base}/contents/{file_path}",
                headers=headers,
                params={"ref": branch},
            )

            if resp.status_code == 200:
                data = resp.json()
                existing = base64.b64decode(data["content"]).decode("utf-8")
                file_sha = data["sha"]
            elif resp.status_code == 404:
                existing = _new_toon_header(file_path)
                file_sha = None
            else:
                return {"pushed": False, "reason": f"GitHub GET failed: {resp.status_code}"}

            # Append rows and update count
            updated = existing
            for row in rows:
                updated = _append_toon_row(updated, row)

            # Get main SHA for branch creation
            ref_resp = await client.get(
                f"{api_base}/git/ref/heads/{branch}",
                headers=headers,
            )
            ref_resp.raise_for_status()
            main_sha = ref_resp.json()["object"]["sha"]

            # Create feature branch
            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            safe_topic = re.sub(r"[^a-z0-9-]", "", topic.lower().replace(" ", "-"))[:30]
            new_branch = f"knowledge/{safe_topic}-{ts}"

            await client.post(
                f"{api_base}/git/refs",
                headers=headers,
                json={"ref": f"refs/heads/{new_branch}", "sha": main_sha},
            )

            # Get file SHA on new branch
            branch_resp = await client.get(
                f"{api_base}/contents/{file_path}",
                headers=headers,
                params={"ref": new_branch},
            )
            branch_file_sha = None
            if branch_resp.status_code == 200:
                branch_file_sha = branch_resp.json()["sha"]

            # Push file
            payload: dict[str, Any] = {
                "message": f"knowledge: add {len(rows)} entries for '{topic}' via Scaffold Engine",
                "content": base64.b64encode(updated.encode("utf-8")).decode("utf-8"),
                "branch": new_branch,
            }
            if branch_file_sha:
                payload["sha"] = branch_file_sha

            put_resp = await client.put(
                f"{api_base}/contents/{file_path}",
                headers=headers,
                json=payload,
            )
            if put_resp.status_code not in (200, 201):
                return {"pushed": False, "reason": f"Push failed: {put_resp.status_code}"}

            # Open PR
            pr_resp = await client.post(
                f"{api_base}/pulls",
                headers=headers,
                json={
                    "title": f"knowledge: add {topic} entries via Scaffold Engine",
                    "head": new_branch,
                    "base": branch,
                    "body": f"Add {len(rows)} TOON entries for `{topic}`.\n\nPushed via Scaffold Engine GT extractor.",
                },
            )
            pr_data = pr_resp.json()

            return {
                "pushed": True,
                "branch": new_branch,
                "pr_number": pr_data.get("number"),
                "pr_url": pr_data.get("html_url", ""),
            }

    except Exception as e:
        logger.error("GitHub push failed: %s", e)
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
        f"  source: https://github.com/LocketKeyLLC/smokieRAGs\n"
        f"  timestamp: {ts}\n"
        f"  content_type: technical-knowledge\n"
        f"  category: {category}\n\n"
        f"knowledge[0]{{id,topic,content,tags,source,verified,last_verified}}:\n"
    )


def _append_toon_row(file_content: str, row: str) -> str:
    clean_row = row.strip()
    if not clean_row.startswith("  "):
        clean_row = f"  {clean_row}"

    pattern = r"(knowledge\[)(\d+)(\]\{[^}]+\}:)"
    match = re.search(pattern, file_content)

    if match:
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
) -> dict:
    """Extract ground truths for a topic via SearXNG + LLM distillation.

    Args:
        topic: The subject to research
        queries: Optional custom search queries (auto-generated if omitted)
        push_to_github: If True, push TOON entries to GitHub KB via PR
        target_file: Target .toon file path (auto-detected if omitted)
        model: Override model for distillation

    Returns:
        Dict with entries, toon_rows, and optional github result
    """
    # 1. Generate search queries if not provided
    if not queries:
        queries = [topic, f"{topic} best practices", f"{topic} technical details"]

    # 2. Search via SearXNG
    all_results: list[dict] = []
    for query in queries[:5]:
        results = await _search_searxng(query)
        all_results.extend(results)
        logger.info("SearXNG: %d results for '%s'", len(results), query)

    if not all_results:
        return {
            "status": "no_results",
            "topic": topic,
            "error": "SearXNG returned no results for any query",
        }

    # 3. Deduplicate by URL
    seen_urls: set[str] = set()
    unique_results: list[dict] = []
    for r in all_results:
        url = r.get("url", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            unique_results.append(r)

    # 4. Distill via LLM
    results_text = "\n\n".join(
        f"Title: {r['title']}\nURL: {r['url']}\nSnippet: {r['content']}"
        for r in unique_results[:15]
    )

    prompt = DISTILL_PROMPT.format(topic=topic, results=results_text)
    resp = await model_router.generate(
        prompt,
        model=model or settings.model_general,
        system=DISTILL_SYSTEM,
        temperature=0.2,
        max_tokens=4096,
    )

    if not resp.success:
        return {
            "status": "llm_failed",
            "topic": topic,
            "error": resp.error,
        }

    # 5. Parse entries
    entries = _parse_entries(resp.text)
    if not entries:
        return {
            "status": "parse_failed",
            "topic": topic,
            "error": "Could not parse LLM output as JSON array",
            "raw_output": resp.text[:500],
        }

    # 6. Format as TOON rows
    toon_rows = _format_toon_rows(entries)

    # 7. Detect target file
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

    # 8. Optional GitHub push
    if push_to_github:
        gh_result = await _push_to_github(toon_rows, target_file, topic)
        result["github"] = gh_result

    return result


def _parse_entries(raw: str) -> list[dict] | None:
    """Parse JSON array from LLM output."""
    return parse_json_array(raw)
