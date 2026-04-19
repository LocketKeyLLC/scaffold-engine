"""GitHub repo ingestion for /research github:owner/repo.

Fetches README, docs/**/*.md, and top-level Python module docstrings,
returning them as {path, content} dicts ready for TOON ingestion.
"""
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

    Shared by github_ingest and gt_extractor (push path).
    """
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
    except Exception as e:
        logger.warning("README decode failed: %s", e)
        return "", ""
    return data.get("path", "README"), content


async def _get_tree(client: httpx.AsyncClient, owner: str, repo: str, branch: str) -> list[dict]:
    r = await client.get(
        f"/repos/{owner}/{repo}/git/trees/{branch}",
        params={"recursive": "1"},
    )
    _check_rate_limit(r)
    r.raise_for_status()
    data = r.json()
    if data.get("truncated"):
        logger.warning("Tree truncated for %s/%s — some files may be missed", owner, repo)
    return [e for e in data.get("tree", []) if e.get("type") == "blob"]


def _select_tree_files(tree: list[dict], remaining_cap: int) -> list[dict]:
    """Pick docs/**/*.md and top-level *.py, capped."""
    docs = [e for e in tree if e["path"].startswith(_DOCS_PREFIX) and e["path"].endswith(".md")]
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
    try:
        return ast.get_docstring(ast.parse(source)) or ""
    except SyntaxError:
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

    # 2. docs/**/*.md + top-level *.py via tree
    remaining = settings.github_max_files - len(results)
    if remaining > 0:
        tree = await _get_tree(client, owner, repo, branch)
        selected = _select_tree_files(tree, remaining)

        for entry in selected:
            path = entry["path"]
            content = await _fetch_blob(client, owner, repo, entry["sha"])
            if not content.strip():
                continue

            # Python files: extract module docstring only
            if path.endswith(".py"):
                docstring = _extract_docstring(content)
                if not docstring:
                    continue
                content = docstring

            results.append({"path": path, "content": content})

    logger.info(
        "GitHub fetch: %s/%s branch=%s files=%d",
        owner, repo, branch, len(results),
    )
    return results
