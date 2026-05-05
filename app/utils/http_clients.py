"""
Shared HTTP clients with connection pooling.

Clients are eager-initialized via init_clients() at app startup (lifespan).
close_clients() is called at shutdown. get_* helpers assert the client exists
and raise if called before init or after close — no lazy creation path.
"""
import httpx
import logging

from app.config import settings

logger = logging.getLogger(__name__)

_clients: dict[str, httpx.AsyncClient] = {}


def _get_or_create(name: str, factory) -> httpx.AsyncClient:
    """Return existing client by name, else build via factory() and store."""
    existing = _clients.get(name)
    if existing is not None and not existing.is_closed:
        return existing
    client = factory()
    _clients[name] = client
    return client


def _build_searxng() -> httpx.AsyncClient:
    client = httpx.AsyncClient(
        base_url=settings.searxng_url,
        timeout=30.0,
        follow_redirects=True,
        limits=httpx.Limits(
            max_connections=10,
            max_keepalive_connections=5,
            keepalive_expiry=30,
        ),
    )
    logger.info("SearXNG client initialized: %s", settings.searxng_url)
    return client


def _build_github() -> httpx.AsyncClient:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "scaffold-engine",
    }
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    client = httpx.AsyncClient(
        base_url=settings.github_api_base,
        timeout=float(settings.github_timeout),
        headers=headers,
        follow_redirects=True,
        limits=httpx.Limits(
            max_connections=10,
            max_keepalive_connections=5,
            keepalive_expiry=30,
        ),
    )
    token_status = "authenticated" if settings.github_token else "unauthenticated (60/hr limit)"
    logger.info("GitHub client initialized: %s (%s)", settings.github_api_base, token_status)
    return client


def _build_generic() -> httpx.AsyncClient:
    # Generic client fans out to arbitrary OpenAPI hosts during /research;
    # raise pool ceiling well above per-host clients.
    client = httpx.AsyncClient(
        timeout=30.0,
        follow_redirects=True,
        headers={"User-Agent": "scaffold-engine"},
        limits=httpx.Limits(
            max_connections=50,
            max_keepalive_connections=20,
            keepalive_expiry=30,
        ),
    )
    logger.info("Generic HTTP client initialized (max_connections=50)")
    return client


def _build_ollama() -> httpx.AsyncClient:
    # Ollama dispatch: per-call timeout overrides the client default
    # (model_router selects cloud_timeout vs local_timeout per request),
    # so we set the client default to local_timeout as a safety net for
    # any caller that forgets to pass an explicit timeout.
    client = httpx.AsyncClient(
        base_url=settings.ollama_base_url,
        timeout=float(settings.local_timeout),
        limits=httpx.Limits(
            max_connections=20,
            max_keepalive_connections=10,
            keepalive_expiry=30,
        ),
    )
    logger.info("Ollama client initialized: %s", settings.ollama_base_url)
    return client


def init_clients() -> None:
    """Eager-init all shared clients. Call once from app lifespan startup."""
    _get_or_create("searxng", _build_searxng)
    _get_or_create("github", _build_github)
    _get_or_create("generic", _build_generic)
    _get_or_create("ollama", _build_ollama)


def get_searxng_client() -> httpx.AsyncClient:
    client = _clients.get("searxng")
    if client is None or client.is_closed:
        raise RuntimeError("SearXNG client not initialized; call init_clients() at startup")
    return client


def get_github_client() -> httpx.AsyncClient:
    client = _clients.get("github")
    if client is None or client.is_closed:
        raise RuntimeError("GitHub client not initialized; call init_clients() at startup")
    return client


def get_generic_http_client() -> httpx.AsyncClient:
    client = _clients.get("generic")
    if client is None or client.is_closed:
        raise RuntimeError("Generic HTTP client not initialized; call init_clients() at startup")
    return client


def get_ollama_client() -> httpx.AsyncClient:
    client = _clients.get("ollama")
    if client is None or client.is_closed:
        raise RuntimeError("Ollama client not initialized; call init_clients() at startup")
    return client


async def close_clients() -> None:
    """Shutdown hook — close each client under try/finally; globals reset unconditionally."""
    for name in list(_clients.keys()):
        client = _clients.get(name)
        try:
            if client is not None and not client.is_closed:
                await client.aclose()
                logger.info("%s client closed", name)
        except Exception:
            logger.exception("Error closing %s client", name)
        finally:
            _clients.pop(name, None)
