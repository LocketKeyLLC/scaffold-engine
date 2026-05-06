"""Ollama provider — thin adapter over ``app.model_router``.

The retry/fallback/timeout logic lives in ``app.model_router._dispatch_with_retry``
and the single-call HTTP path lives in ``app.model_router._call_ollama``. This
class delegates to those functions instead of re-implementing them so that:

  1. Tests that ``patch.object(model_router, "_call_ollama", ...)`` continue
     to intercept every dispatch path — including calls reached through
     ``provider_for_role``.
  2. Behavior stays bit-identical for existing callers; the provider
     abstraction only adds a routing seam, not a second implementation.

Adding non-Ollama providers later (OpenAI, Anthropic, …) means writing a
self-contained module — those backends do not share Ollama's HTTP shape so
they get their own dispatch.
"""
from __future__ import annotations

import logging
from typing import Any

from app.providers.base import LLMProvider, ModelResponse

logger = logging.getLogger("scaffold.providers.ollama")


class OllamaProvider(LLMProvider):
    """Local-or-bridge Ollama backend. Delegates to ``app.model_router``."""

    name = "ollama"
    supports_chat = True
    supports_embeddings = True
    supports_streaming = True
    supports_native_tools = False

    async def chat_completion(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        timeout: int = 600,  # noqa: ARG002 — model_router resolves cloud vs local
        fallback: str | None = None,
        **opts: Any,
    ) -> ModelResponse:
        from app import model_router
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        return await model_router._dispatch_with_retry(
            "/api/chat", payload, model, fallback,
        )

    async def generate(
        self,
        model: str,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        timeout: int = 600,  # noqa: ARG002 — model_router resolves cloud vs local
        fallback: str | None = None,
        **opts: Any,
    ) -> ModelResponse:
        from app import model_router
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if system:
            payload["system"] = system
        return await model_router._dispatch_with_retry(
            "/api/generate", payload, model, fallback,
        )

    async def embed(
        self,
        model: str,
        texts: list[str],
        *,
        timeout: int = 120,  # noqa: ARG002 — model_router resolves cloud vs local
    ) -> list[list[float]]:
        from app import model_router
        payload: dict[str, Any] = {
            "model": model,
            "input": texts,
        }
        resp = await model_router._dispatch_with_retry(
            "/api/embed", payload, model, fallback=None,
        )
        if not resp.success:
            logger.error("Embedding failed after retries: %s", resp.error)
            return []
        return resp.raw.get("embeddings", [])

    async def list_models(self) -> list[str]:
        from app import model_router
        return await model_router.list_models()


# Register the singleton at import time. ``app/providers/__init__.py``
# triggers this via its ``_autoload`` helper.
from app.providers import register  # noqa: E402

register("ollama", OllamaProvider())
