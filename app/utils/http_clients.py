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
    global _searxng_client
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


async def close_clients() -> None:
    """Shutdown hook — close all shared clients."""
    global _searxng_client
    if _searxng_client and not _searxng_client.is_closed:
        await _searxng_client.aclose()
        _searxng_client = None
        logger.info("SearXNG client closed")
