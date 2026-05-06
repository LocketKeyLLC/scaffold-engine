"""Provider abstraction for LLM backends.

Each backend (Ollama, OpenAI, Anthropic, HuggingFace, …) implements
``LLMProvider`` so per-role config can pick a backend without changing
call sites in the orchestrator.

Public surface stays in ``app/model_router.py`` — those callers receive
``ModelResponse`` regardless of provider.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

logger = logging.getLogger("scaffold.providers")


# ---------------------------------------------------------------------------
# Capability errors — providers raise these instead of silently returning
# empty/None results so the dispatcher can surface a clear failure to the
# orchestrator and the user.
# ---------------------------------------------------------------------------


class ProviderError(Exception):
    """Base provider error."""


class ProviderCapabilityError(ProviderError):
    """Raised when a role is bound to a provider that lacks the capability.

    Example: Anthropic does not expose an embeddings endpoint; assigning the
    embedder role to AnthropicProvider raises this at boot.
    """


class ProviderUnavailableError(ProviderError):
    """Raised when a provider is reachable but returns a non-recoverable error
    (auth, rate-limit exceeded, model not found)."""


class ProviderTimeoutError(ProviderError):
    """Raised when a provider call exceeds the configured timeout."""


# ---------------------------------------------------------------------------
# Unified response shape — preserved verbatim from the original
# model_router.ModelResponse so callers don't change. Providers populate
# whatever fields they have access to; missing fields stay None.
# ---------------------------------------------------------------------------


@dataclass
class ModelResponse:
    """Provider-agnostic response container."""

    text: str = ""
    model: str = ""
    success: bool = True
    error: str | None = None
    ttft_ms: int | None = None
    total_duration_ms: int = 0
    tokens_prompt: int | None = None
    tokens_completion: int | None = None
    tokens_per_sec: float | None = None
    retries: int = 0
    fallback_used: bool = False
    provider: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Capability flags — declared as class attributes so the dispatcher can
# validate role assignments before issuing a call.
# ---------------------------------------------------------------------------


class LLMProvider(ABC):
    """Abstract LLM backend.

    Subclasses set the ``name`` and ``supports_*`` attributes and implement
    the required async methods. Dispatcher reads ``supports_*`` to decide
    whether a (role, provider) binding is legal at boot time.
    """

    # Identifier matching ``MODEL_*_PROVIDER`` settings ("ollama", "openai", ...).
    name: str = ""

    # Default capabilities. Overridden in subclasses.
    supports_chat: bool = True
    supports_embeddings: bool = False
    supports_streaming: bool = True
    supports_native_tools: bool = False

    # ------------------------------------------------------------------
    # Required async methods
    # ------------------------------------------------------------------

    @abstractmethod
    async def chat_completion(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        timeout: int = 600,
        **opts: Any,
    ) -> ModelResponse:
        """One-shot chat completion. Returns ``ModelResponse``.

        ``messages`` follows the OpenAI shape: ``[{"role": "user|system|
        assistant", "content": "..."}, ...]``. Providers translate to
        their native shape internally.
        """
        ...

    async def generate(
        self,
        model: str,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        timeout: int = 600,
        **opts: Any,
    ) -> ModelResponse:
        """Single-prompt completion.

        Default builds an OpenAI-shape messages array and delegates to
        ``chat_completion``. Providers with a more efficient native
        completion endpoint (e.g. Ollama's ``/api/generate``) may override.
        """
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return await self.chat_completion(
            model, messages,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            **opts,
        )

    async def embed(
        self,
        model: str,
        texts: list[str],
        *,
        timeout: int = 120,
    ) -> list[list[float]]:
        """Return one embedding vector per input text.

        Default implementation raises ``ProviderCapabilityError`` —
        providers that support embeddings override this.
        """
        raise ProviderCapabilityError(
            f"{self.name} provider does not support embeddings"
        )

    async def stream_chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        timeout: int = 600,
        **opts: Any,
    ) -> AsyncIterator[str]:
        """Yield chunks of generated text as they arrive.

        Default implementation raises ``ProviderCapabilityError`` —
        providers that support streaming override this.
        """
        raise ProviderCapabilityError(
            f"{self.name} provider does not support streaming"
        )
        # The yield below is unreachable but tells the type-checker this
        # is an async generator (the abstract default still has the right
        # call signature).
        yield ""  # pragma: no cover

    @abstractmethod
    async def list_models(self) -> list[str]:
        """Return the list of model identifiers this provider can serve."""
        ...

    async def health_check(self) -> dict[str, Any]:
        """Lightweight reachability probe.

        Default implementation tries ``list_models`` and reports the
        round-trip time. Providers may override with a cheaper probe.
        """
        import time as _time
        start = _time.monotonic()
        try:
            models = await self.list_models()
            elapsed_ms = int((_time.monotonic() - start) * 1000)
            return {
                "provider": self.name,
                "status": "up",
                "latency_ms": elapsed_ms,
                "models_available": len(models),
            }
        except Exception as exc:
            elapsed_ms = int((_time.monotonic() - start) * 1000)
            return {
                "provider": self.name,
                "status": "down",
                "latency_ms": elapsed_ms,
                "error": str(exc)[:200],
            }
