"""GitHub repo ingestion for /research github:owner/repo.

Fetches README, docs/**/*.md, and top-level Python module docstrings,
returning them as {path, content} dicts ready for TOON ingestion.
"""
import asyncio
import ast
import base64
import json
import logging
from typing import Any

import httpx
import redis.asyncio as aioredis

from app.config import settings
from app.utils.http_clients import get_github_client

logger = logging.getLogger(__name__)

_DOCS_PREFIX = "docs/"

# Audit M6 — versioned cache key prefix for the tree response cache. Bumping
# the version invalidates every cached entry (use when the cached payload
# shape changes, not when GitHub data changes — that's what ETags are for).
_GITHUB_TREE_CACHE_KEY_PREFIX = "github:tree:v1"

_redis: aioredis.Redis | None = None


async def _redis_client() -> aioredis.Redis | None:
    """Lazy-init Redis client for the tree cache.

    Returns None if the cache is disabled (TTL=0) or if the connection
    init fails. All callers fail-open: a None client means "skip the
    cache, do a live API call."
    """
    global _redis
    if settings.github_tree_cache_ttl_seconds <= 0:
        return None
    if _redis is None:
        try:
            _redis = aioredis.from_url(settings.redis_url, decode_responses=True)
        except Exception as exc:
            logger.warning("github_ingest: redis init failed, cache disabled: %s", exc)
            return None
    return _redis


def _tree_cache_key(owner: str, repo: str, branch: str) -> str:
    return f"{_GITHUB_TREE_CACHE_KEY_PREFIX}:{owner}/{repo}:{branch}"


async def _read_cached_tree(
    owner: str, repo: str, branch: str,
) -> tuple[str, list[dict], bool] | None:
    """Return (etag, blobs, truncated) from cache, or None on miss/error/disabled."""
    r = await _redis_client()
    if r is None:
        return None
    try:
        raw = await r.get(_tree_cache_key(owner, repo, branch))
        if not raw:
            return None
        entry = json.loads(raw)
        return entry["etag"], entry["blobs"], entry["truncated"]
    except Exception as exc:
        logger.debug("github_ingest: tree cache read failed: %s", exc)
        return None


async def _write_cached_tree(
    owner: str, repo: str, branch: str,
    etag: str, blobs: list[dict], truncated: bool,
) -> None:
    r = await _redis_client()
    if r is None:
        return
    try:
        payload = json.dumps({"etag": etag, "blobs": blobs, "truncated": truncated})
        await r.set(
            _tree_cache_key(owner, repo, branch),
            payload,
            ex=settings.github_tree_cache_ttl_seconds,
        )
    except Exception as exc:
        logger.debug("github_ingest: tree cache write failed: %s", exc)


async def _refresh_cached_tree_ttl(owner: str, repo: str, branch: str) -> None:
    """Touch an existing entry's TTL on a 304 hit — keeps hot entries hot."""
    r = await _redis_client()
    if r is None:
        return
    try:
        await r.expire(
            _tree_cache_key(owner, repo, branch),
            settings.github_tree_cache_ttl_seconds,
        )
    except Exception as exc:
        logger.debug("github_ingest: tree cache TTL refresh failed: %s", exc)


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
    """Fetch repo tree, with optional Redis cache backed by GitHub ETag.

    Audit M6 (closes #151). Cache stores ``(etag, blobs, truncated)`` per
    ``(owner, repo, branch)``. On hit we send ``If-None-Match: <etag>``;
    GitHub returns 304 (free — doesn't count against rate limit) when the
    tree hasn't changed, and we return the cached blobs unchanged. On 200
    we re-parse and refresh the cache. Cache failures fail-open to a
    normal live call.
    """
    cached = await _read_cached_tree(owner, repo, branch)
    headers = {"If-None-Match": cached[0]} if cached is not None else None

    r = await client.get(
        f"/repos/{owner}/{repo}/git/trees/{branch}",
        params={"recursive": "1"},
        headers=headers,
    )
    _check_rate_limit(r)

    if r.status_code == 304 and cached is not None:
        # GitHub confirms unchanged — return cached payload, refresh TTL.
        _, cached_blobs, cached_truncated = cached
        await _refresh_cached_tree_ttl(owner, repo, branch)
        return cached_blobs, cached_truncated

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

    etag = r.headers.get("etag")
    if etag:
        await _write_cached_tree(owner, repo, branch, etag, blobs, truncated)
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
            # CancelledError is a BaseException (Py3.8+), not Exception, so
            # the broader catch below does NOT cover it; without this branch
            # a cancelled child slips through as a non-dict result and
            # crashes downstream consumers. Re-raise so cancellation
            # propagates to the caller's await.
            if isinstance(item, asyncio.CancelledError):
                raise item
            if isinstance(item, (GitHubRateLimitError, GitHubRepoNotFoundError)):
                # Critical errors must NOT be swallowed — propagate so caller
                # sees the real failure mode instead of a silent partial result.
                raise item
            if isinstance(item, BaseException):
                # Any other exception (including unexpected BaseException
                # subclasses) is logged and skipped — the task that raised
                # is dropped from results but the rest of the fetch succeeds.
                logger.warning("Blob fetch failed (transient): %s", item)
                continue
            if item is not None:
                results.append(item)

    logger.info(
        "GitHub fetch: %s/%s branch=%s attempted=%d files=%d tree_truncated=%s",
        owner, repo, branch, attempted, len(results), tree_truncated,
    )
    return results
