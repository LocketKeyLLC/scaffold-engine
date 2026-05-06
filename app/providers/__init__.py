"""Provider registry.

Exposes ``get_provider(name)`` and ``provider_for_role(role, overrides)``
to the rest of the orchestrator. Concrete providers register themselves
on import so adding a new backend is a one-line ``register()`` call.
"""
from __future__ import annotations

import logging
from typing import Type

from app.providers.base import (
    LLMProvider,
    ModelResponse,
    ProviderCapabilityError,
    ProviderError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

logger = logging.getLogger("scaffold.providers.registry")

__all__ = [
    "LLMProvider",
    "ModelResponse",
    "ProviderError",
    "ProviderCapabilityError",
    "ProviderUnavailableError",
    "ProviderTimeoutError",
    "register",
    "get_provider",
    "provider_for_role",
    "available_providers",
]


# ---------------------------------------------------------------------------
# Registry storage — populated by import-time register() calls in each
# concrete provider module.
# ---------------------------------------------------------------------------

_PROVIDERS: dict[str, LLMProvider] = {}


def register(name: str, provider: LLMProvider) -> None:
    """Register a singleton provider instance under ``name``.

    Re-registration (same name, new instance) replaces the old singleton —
    useful for tests that swap in a mock without restarting the process.
    """
    if not name:
        raise ValueError("provider name must be non-empty")
    _PROVIDERS[name] = provider
    logger.info("provider_registered: name=%s class=%s", name, type(provider).__name__)


def get_provider(name: str) -> LLMProvider:
    """Return the registered provider singleton for ``name``.

    Raises ``ProviderError`` if no provider is registered under that name.
    """
    p = _PROVIDERS.get(name)
    if p is None:
        raise ProviderError(
            f"unknown provider {name!r}; registered: {sorted(_PROVIDERS.keys())}"
        )
    return p


def available_providers() -> list[str]:
    """List all registered provider names."""
    return sorted(_PROVIDERS.keys())


def provider_for_role(role: str, overrides: dict | None = None) -> LLMProvider:
    """Resolve which provider serves a given model role.

    Precedence:
      1. ``overrides[f"{role}_provider"]`` — request-time override.
      2. ``settings.{role}_provider`` — env var via Pydantic Settings.
      3. ``"ollama"`` — default.

    The returned provider is validated against the role's capability
    requirements: embedder roles require ``supports_embeddings``;
    other roles require ``supports_chat``. Mismatches raise
    ``ProviderCapabilityError`` so misconfiguration fails fast at boot
    rather than silently mid-pipeline.
    """
    from app.config import settings

    key = f"{role}_provider"
    name: str | None = None
    if overrides and key in overrides:
        name = overrides[key]
    if not name:
        name = getattr(settings, key, None)
    if not name:
        name = "ollama"

    provider = get_provider(name)

    # Capability gate: embedder role demands embeddings support.
    if role == "model_embedder_pipeline" and not provider.supports_embeddings:
        raise ProviderCapabilityError(
            f"role {role!r} requires embeddings support; provider "
            f"{name!r} does not. Pick one of: "
            f"{[n for n in available_providers() if get_provider(n).supports_embeddings]}"
        )
    # Reranker is its own world — handled outside the provider system.
    if role != "model_reranker" and not provider.supports_chat:
        raise ProviderCapabilityError(
            f"role {role!r} requires chat support; provider {name!r} does not"
        )
    return provider


# ---------------------------------------------------------------------------
# Eager import of bundled providers — each module runs ``register()`` at
# import time. Adding a new provider: create the module, call ``register``,
# add a one-line import here.
# ---------------------------------------------------------------------------


def _autoload() -> None:
    """Import bundled provider modules so they self-register.

    Wrapped in a function (not import-time) so test fixtures can swap the
    registry without picking up partial state.
    """
    # Each import has a side effect: ``register("name", instance)``.
    # Failures are isolated — if (e.g.) ``cohere`` SDK isn't installed,
    # only that provider is unregistered; the rest still work.
    for mod in (
        "app.providers.ollama",
    ):
        try:
            __import__(mod)
        except Exception as exc:
            logger.warning("provider_autoload_failed: module=%s error=%s", mod, exc)


_autoload()
