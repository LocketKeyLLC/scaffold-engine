"""GitHub repo ingestion for /research github:owner/repo.

Fetches README, docs/**/*.md, and top-level Python module docstrings,
returning them as {path, content} dicts ready for TOON ingestion.
"""
import asyncio
import ast
import base64
import logging
from typing import Any

import httpx

from app.config import settings
from app.utils.http_clients import get_github_client

logger = logging.getLogger(__name__)

_DOCS_PREFIX = "docs/"


class GitHubRepoNotFoundError(Exception):
    """Repo inaccessible (404 — wrong owner/repo, or private without token)."""


class GitHubRateLimitError(Exception):
    """Rate limit exhausted."""


def check_github_rate_limit(response: httpx.Response) -> None:
    """Inspect GitHub response headers and raise if rate limit exhausted.

    Shared by github_ingest and gt_extractor (push path). Also upgrades an
    HTTP 429 response into a GitHubRateLimitError so callers get one consistent
    exception type for all rate-limit situations (#70).
    """
    if response.status_code == 429:
        reset = response.headers.get("X-RateLimit-Reset", "unknown")
        retry_after = response.headers.get("Retry-After", "unknown")
        raise GitHubRateLimitError(
            f"GitHub returned 429. Reset={reset} Retry-After={retry_after}"
        )
    remaining = response.headers.get("X-RateLimit-Remaining")
    if remaining is None:
        return
    remaining_int = int(remaining)
    if remaining_int == 0:
        reset = response.headers.get("X-RateLimit-Reset", "unknown")
        raise GitHubRateLimitError(f"GitHub rate limit exhausted. Resets at {reset}")
    if remaining_int < 10:
        logger.warning("GitHub rate limit low: %d remaining", remaining_int)


# Backward-compat alias for internal callers
_check_rate_limit = check_github_rate_limit


async def _get_default_branch(client: httpx.AsyncClient, owner: str, repo: str) -> str:
    r = await client.get(f"/repos/{owner}/{repo}")
    _check_rate_limit(r)
    if r.status_code == 404:
        raise GitHubRepoNotFoundError(f"{owner}/{repo} not found or inaccessible")
    r.raise_for_status()
    return r.json().get("default_branch", "main")


async def _fetch_readme(client: httpx.AsyncClient, owner: str, repo: str) -> tuple[str, str]:
    """Returns (path, content) or ('', '') if no README."""
    r = await client.get(f"/repos/{owner}/{repo}/readme")
    _check_rate_limit(r)
    if r.status_code == 404:
        return "", ""
    r.raise_for_status()
    data = r.json()
    try:
        content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    except (ValueError, TypeError, KeyError) as e:
        # Distinguish decode failure from "no README" — caller logs and continues,
        # but does not silently treat a corrupt README as missing.
        logger.error("README decode failed for %s/%s: %s", owner, repo, e)
        raise
    return data.get("path", "README"), content


async def _get_tree(client: httpx.AsyncClient, owner: str, repo: str, branch: str) -> tuple[list[dict], bool]:
    # TODO(#151): Cache tree response in Redis keyed by (owner, repo, branch, sha)
    # to avoid repeated GitHub API calls on re-ingests of the same repo.
    # Deferred — current call volume is low and rate limit headroom is adequate.
    r = await client.get(
        f"/repos/{owner}/{repo}/git/trees/{branch}",
        params={"recursive": "1"},
    )
    _check_rate_limit(r)
    r.raise_for_status()
    data = r.json()
    truncated = bool(data.get("truncated"))
    if truncated:
        # #68 — escalate to ERROR so log alerts notice; the SSE layer can
        # surface the _truncated marker to the user via fetch_repo_content.
        logger.error(
            "GitHub tree truncated for %s/%s — results are INCOMPLETE (repo exceeds API tree cap)",
            owner, repo,
        )
    blobs = [e for e in data.get("tree", []) if e.get("type") == "blob"]
    # Return (blobs, truncated) — caller decides whether to surface/raise
    return blobs, truncated


def _select_tree_files(tree: list[dict], remaining_cap: int) -> list[dict]:
    """Pick docs/**/*.md and top-level *.py, capped.

    Design note: only *top-level* .py files are included (no `/` in path). This
    is intentional — a repo's top-level modules are almost always the public
    entry points whose docstrings summarize the package. Recursing into all
    .py files tends to pull in tests, build scripts, and vendored code with
    low signal-to-noise. If you need deeper coverage, prefer adding content
    to docs/ so it is captured by the .md filter.
    """
    docs = [e for e in tree if e["path"].endswith(".md") and (e["path"].startswith(_DOCS_PREFIX) or "/" not in e["path"])]
    pyfiles = [e for e in tree if e["path"].endswith(".py") and "/" not in e["path"]]
    selected = docs + pyfiles
    if len(selected) > remaining_cap:
        logger.warning("File count %d exceeds remaining cap %d — truncating", len(selected), remaining_cap)
        selected = selected[:remaining_cap]
    return selected


async def _fetch_blob(client: httpx.AsyncClient, owner: str, repo: str, sha: str) -> str:
    r = await client.get(f"/repos/{owner}/{repo}/git/blobs/{sha}")
    _check_rate_limit(r)
    r.raise_for_status()
    data = r.json()
    if data.get("encoding") != "base64":
        return ""
    try:
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning("Blob decode failed: %s", e)
        return ""


def _extract_docstring(source: str) -> str:
    # #71 — widen catch: malformed sources can raise ValueError (null bytes)
    # or TypeError (bad ast input) in addition to SyntaxError
    try:
        return ast.get_docstring(ast.parse(source)) or ""
    except (SyntaxError, ValueError, TypeError) as e:
        logger.debug("docstring extract failed: %s", e)
        return ""


async def fetch_repo_content(owner: str, repo: str) -> list[dict[str, Any]]:
    """Fetch ingestible content from a GitHub repo.

    Returns list of {path, content} dicts, capped at settings.github_max_files.

    Raises:
        GitHubRepoNotFoundError: repo 404
        GitHubRateLimitError: rate limit exhausted
    """
    client = get_github_client()
    branch = await _get_default_branch(client, owner, repo)
    results: list[dict[str, Any]] = []

    # 1. README (dedicated endpoint — handles case/extension automatically)
    readme_path, readme_content = await _fetch_readme(client, owner, repo)
    if readme_content.strip():
        results.append({"path": readme_path, "content": readme_content})
    elif readme_path:  # README endpoint returned something but body is empty/whitespace
        logger.warning(
            "GitHub README is empty/whitespace-only, dropping: %s/%s path=%s",
            owner, repo, readme_path,
        )

    # 2. docs/**/*.md + top-level *.py via tree
    tree_truncated = False
    attempted = 0
    remaining = settings.github_max_files - len(results)
    if remaining > 0:
        tree, tree_truncated = await _get_tree(client, owner, repo, branch)
        selected = _select_tree_files(tree, remaining)
        attempted = len(selected)

        # #69 — parallelize blob fetches. Semaphore bounds concurrent GitHub
        # calls so we don't blow through the rate limit in bursts.
        sem = asyncio.Semaphore(settings.github_blob_concurrency)

        async def _fetch_one(entry: dict) -> dict | None:
            path = entry["path"]
            async with sem:
                content = await _fetch_blob(client, owner, repo, entry["sha"])
            if not content.strip():
                return None
            if path.endswith(".py"):
                docstring = _extract_docstring(content)
                if not docstring:
                    return None
                content = docstring
            return {"path": path, "content": content}

        fetched = await asyncio.gather(
            *(_fetch_one(e) for e in selected), return_exceptions=True,
        )
        for item in fetched:
            if isinstance(item, (GitHubRateLimitError, GitHubRepoNotFoundError)):
                # Critical errors must NOT be swallowed — propagate so caller
                # sees the real failure mode instead of a silent partial result.
                raise item
            if isinstance(item, Exception):
                logger.warning("Blob fetch failed (transient): %s", item)
                continue
            if item is not None:
                results.append(item)

    logger.info(
        "GitHub fetch: %s/%s branch=%s attempted=%d files=%d tree_truncated=%s",
        owner, repo, branch, attempted, len(results), tree_truncated,
    )
    return results
