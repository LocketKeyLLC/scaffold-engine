"""
Shared HTTP clients with connection pooling.

Usage:
    from app.utils.http_clients import get_searxng_client

SearXNG client is lazy-initialized on first call.
Call close_clients() during app shutdown.
"""

import httpx
import logging

from app.config import settings

logger = logging.getLogger(__name__)

_searxng_client: httpx.AsyncClient | None = None


def get_searxng_client() -> httpx.AsyncClient:
    """Return the module-level SearXNG async client (lazy init)."""
    global _searxng_client, _github_client
    if _searxng_client is None or _searxng_client.is_closed:
        _searxng_client = httpx.AsyncClient(
            base_url=settings.searxng_url,
            timeout=30.0,
            limits=httpx.Limits(
                max_connections=10,
                max_keepalive_connections=5,
                keepalive_expiry=30,
            ),
        )
        logger.info("SearXNG client initialized: %s", settings.searxng_url)
    return _searxng_client


_github_client: httpx.AsyncClient | None = None


def get_github_client() -> httpx.AsyncClient:
    """Return the module-level GitHub API async client (lazy init)."""
    global _github_client
    if _github_client is None or _github_client.is_closed:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "scaffold-engine",
        }
        if settings.github_token:
            headers["Authorization"] = f"Bearer {settings.github_token}"
        _github_client = httpx.AsyncClient(
            base_url=settings.github_api_base,
            timeout=float(settings.github_timeout),
            headers=headers,
            limits=httpx.Limits(
                max_connections=10,
                max_keepalive_connections=5,
                keepalive_expiry=30,
            ),
        )
        token_status = "authenticated" if settings.github_token else "unauthenticated (60/hr limit)"
        logger.info("GitHub client initialized: %s (%s)", settings.github_api_base, token_status)
    return _github_client


async def close_clients() -> None:
    """Shutdown hook — close all shared clients."""
    global _searxng_client, _github_client
    if _searxng_client and not _searxng_client.is_closed:
        await _searxng_client.aclose()
        _searxng_client = None
        logger.info("SearXNG client closed")
    if _github_client and not _github_client.is_closed:
        await _github_client.aclose()
        _github_client = None
        logger.info("GitHub client closed")
