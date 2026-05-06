"""Scaffold Engine — Model router.

Ollama dispatch with:
  - Timeout handling (cloud vs local)
  - Retry cascade (3x same model → fallback swap)
  - Performance metric capture (for Step 9 middleware)

Step 7 of 23-step build plan.

Sprint E refactor: ``ModelResponse`` is now defined in
``app.providers.base`` and re-exported here so existing imports keep
working. Concrete provider classes live in ``app.providers.*``;
``OllamaProvider`` delegates back into this module so tests that
``patch.object(model_router, "_call_ollama", ...)`` keep intercepting
all dispatch paths — including those reached through the provider
abstraction. The provider abstraction is what later sprints (E.7+)
use to route per-role calls to non-Ollama backends.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import httpx

from app.config import settings
from app.providers.base import ModelResponse  # noqa: F401 — public re-export

logger = logging.getLogger("scaffold.router")

# ---------------------------------------------------------------------------
# Ollama HTTP client — delegates to the shared pool in app.utils.http_clients
# (initialized eagerly at app lifespan startup; closed by close_clients()).
# ---------------------------------------------------------------------------


def _get_client() -> httpx.AsyncClient:
    """Return the shared Ollama AsyncClient.

    Kept as a thin wrapper rather than importing ``get_ollama_client``
    everywhere so existing tests that ``patch.object(model_router,
    "_get_client", return_value=...)`` continue to work without
    modification.
    """
    from app.utils.http_clients import get_ollama_client
    return get_ollama_client()


async def close_client() -> None:
    """No-op retained for backward compatibility.

    The Ollama client is owned by ``app.utils.http_clients`` and is
    closed by its ``close_clients()`` shutdown hook. This function is
    preserved so existing callers in main.py's lifespan don't break;
    new code should rely on ``close_clients()`` directly.
    """
    return None

# ---------------------------------------------------------------------------
# Cloud model detection
# ---------------------------------------------------------------------------

CLOUD_MODELS = frozenset({
    settings.model_cloud_heavy,
    settings.model_cloud_alt,
})


def _is_cloud(model: str) -> bool:
    return model in CLOUD_MODELS or model.endswith("-cloud")

def _timeout_for(model: str) -> int:
    return settings.cloud_timeout if _is_cloud(model) else settings.local_timeout


def _smart_fallback(model: str, default_fallback: str) -> str:
    """Map non-existent models to appropriate fallbacks."""
    model_lower = model.lower()
    # Code-related models → coder
    if any(x in model_lower for x in ["code", "coder", "codegen"]):
        return settings.model_coder
    return default_fallback


# ---------------------------------------------------------------------------
# Core dispatch
# ---------------------------------------------------------------------------

async def _call_ollama(
    endpoint: str,
    payload: dict[str, Any],
    model: str,
    timeout: int,
) -> ModelResponse:
    """Single Ollama API call. Returns ModelResponse regardless of outcome."""
    url = f"{settings.ollama_base_url}{endpoint}"
    start = time.monotonic()
    try:
        client = _get_client()
        resp = await client.post(url, json=payload, timeout=timeout)
        elapsed_ms = int((time.monotonic() - start) * 1000)

        if resp.status_code != 200:
            return ModelResponse(
                model=model,
                success=False,
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                total_duration_ms=elapsed_ms,
                provider="ollama",
            )

        data = resp.json()
        # Extract text from the response format
        text = (
            data.get("response")            # /api/generate
            or data.get("message", {}).get("content", "")  # /api/chat
            or ""
        )

        # Extract performance metrics from Ollama response
        tokens_prompt = data.get("prompt_eval_count")
        tokens_completion = data.get("eval_count")
        # Ollama reports durations in nanoseconds
        ttft_ns = data.get("prompt_eval_duration")
        ttft_ms = int(ttft_ns / 1_000_000) if ttft_ns else None
        eval_duration_ns = data.get("eval_duration")
        tps = None
        if tokens_completion and eval_duration_ns and eval_duration_ns > 0:
            tps = round(tokens_completion / (eval_duration_ns / 1e9), 2)

        return ModelResponse(
            text=text.strip(),
            model=model,
            success=True,
            ttft_ms=ttft_ms,
            total_duration_ms=elapsed_ms,
            tokens_prompt=tokens_prompt,
            tokens_completion=tokens_completion,
            tokens_per_sec=tps,
            provider="ollama",
            raw=data,
            )

    except httpx.TimeoutException:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return ModelResponse(
            model=model,
            success=False,
            error=f"Timeout after {timeout}s",
            total_duration_ms=elapsed_ms,
            provider="ollama",
        )
    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return ModelResponse(
            model=model,
            success=False,
            error=str(e),
            total_duration_ms=elapsed_ms,
            provider="ollama",
        )


# ---------------------------------------------------------------------------
# Retry + fallback wrapper
# ---------------------------------------------------------------------------

async def _dispatch_with_retry(
    endpoint: str,
    payload: dict[str, Any],
    model: str,
    fallback: str | None = None,
    max_retries: int | None = None,
) -> ModelResponse:
    """Retry cascade: up to max_retries on primary, then swap to fallback."""
    retries = max_retries if max_retries is not None else settings.max_retries
    fallback = fallback or _smart_fallback(model, settings.model_fallback)

    # Phase 1: retry primary model
    last_resp = ModelResponse(model=model, success=False, error="no attempt", provider="ollama")
    for attempt in range(retries):
        payload["model"] = model
        last_resp = await _call_ollama(
            endpoint, payload, model, _timeout_for(model),
        )
        if last_resp.success:
            last_resp.retries = attempt
            return last_resp
        logger.warning(
            "Attempt %d/%d failed for %s: %s",
            attempt + 1, retries, model, last_resp.error,
        )

    # Phase 2: fallback (skip if fallback == primary)
    if fallback and fallback != model:
        logger.info("Falling back from %s → %s", model, fallback)
        payload["model"] = fallback
        fb_resp = await _call_ollama(
            endpoint, payload, fallback, _timeout_for(fallback),
        )
        fb_resp.retries = retries
        fb_resp.fallback_used = True
        if fb_resp.success:
            return fb_resp
        last_resp = fb_resp

    # All attempts exhausted
    last_resp.retries = retries
    logger.error("All attempts exhausted for %s (fallback: %s)", model, fallback)
    return last_resp


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def generate(
    prompt: str,
    model: str | None = None,
    *,
    system: str | None = None,
    temperature: float = 0.7,
    max_tokens: int = 4096,
    fallback: str | None = None,
) -> ModelResponse:
    """Generate text with /api/generate."""
    model = model or settings.model_general
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }
    if system:
        payload["system"] = system
    return await _dispatch_with_retry("/api/generate", payload, model, fallback)


async def chat(
    messages: list[dict[str, str]],
    model: str | None = None,
    *,
    temperature: float = 0.7,
    max_tokens: int = 4096,
    fallback: str | None = None,
) -> ModelResponse:
    """Chat completion with /api/chat."""
    model = model or settings.model_general
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }
    return await _dispatch_with_retry("/api/chat", payload, model, fallback)


async def embed(
    text: str | list[str],
    model: str | None = None,
) -> list[list[float]]:
    """Get embeddings via /api/embed. Returns list of vectors."""
    model = model or settings.model_embedder_pipeline
    inputs = text if isinstance(text, list) else [text]
    payload: dict[str, Any] = {
        "model": model,
        "input": inputs,
    }
    resp = await _dispatch_with_retry("/api/embed", payload, model, fallback=None)
    if not resp.success:
        logger.error("Embedding failed after retries: %s", resp.error)
        return []
    return resp.raw.get("embeddings", [])


async def classify(
    prompt: str,
    model: str | None = None,
) -> ModelResponse:
    """Lightweight classification/routing call (low temperature)."""
    model = model or settings.model_router
    return await generate(
        prompt, model, temperature=0.1, max_tokens=256, fallback=None,
    )


async def list_models() -> list[str]:
    """Return list of available Ollama models."""
    try:
        client = _get_client()
        resp = await client.get(f"{settings.ollama_base_url}/api/tags")
        return [m["name"] for m in resp.json().get("models", [])]
    except Exception as e:
        logger.error("Failed to list models: %s", e)
        return []


async def validate_models(overrides: dict | None = None) -> Optional[list[str]]:
    """
    Check that all Ollama-routed model roles resolve to tags that exist in Ollama.
    Returns:
        list[str]: missing role=tag pairs (empty list => all present)
        None: Ollama unreachable / HTTP error / malformed response
    """
    from app.config import get_model

    OLLAMA_ROLES = [
        "model_general", "model_verifier", "model_coder",
        "model_router", "model_fallback", "model_cloud_alt",
    ]
    needed = {role: get_model(role, overrides) for role in OLLAMA_ROLES}

    try:
        resp = await _get_client().get(
            f"{settings.ollama_base_url}/api/tags", timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error("validate_models: Ollama unreachable: %s", e)
        return None

    available = set()
    for model in data.get("models", []):
        name = model.get("name", "")
        available.add(name)
        if name.endswith(":latest"):
            available.add(name.removesuffix(":latest"))

    missing = []
    for role, tag in needed.items():
        if tag not in available and f"{tag}:latest" not in available:
            missing.append(f"{role}={tag}")

    return missing
