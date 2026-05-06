"""Tests for app/providers/ — registry, capability gate, autoload.

Sprint E. The registry is module-level state so each test snapshots
``_PROVIDERS`` and restores it on teardown to keep tests isolated.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app import providers as registry
from app.providers.base import (
    LLMProvider,
    ModelResponse,
    ProviderCapabilityError,
    ProviderError,
)
from app.providers.ollama import OllamaProvider


# ---------------------------------------------------------------------------
# Registry isolation — autouse so any registration done inside a test gets
# rolled back even if the test raises. Mirrors the pattern used for
# http_clients in conftest.py.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _snapshot_registry():
    saved = dict(registry._PROVIDERS)
    try:
        yield
    finally:
        registry._PROVIDERS.clear()
        registry._PROVIDERS.update(saved)


# ---------------------------------------------------------------------------
# Test helpers — minimal LLMProvider stubs with controlled capabilities.
# ---------------------------------------------------------------------------
class _ChatOnlyProvider(LLMProvider):
    name = "chatonly"
    supports_chat = True
    supports_embeddings = False
    supports_streaming = False
    supports_native_tools = False

    async def chat_completion(self, model, messages, **opts) -> ModelResponse:
        return ModelResponse(model=model, success=True, provider=self.name)

    async def list_models(self) -> list[str]:
        return ["chatonly-1"]


class _EmbedOnlyProvider(LLMProvider):
    name = "embedonly"
    supports_chat = False
    supports_embeddings = True
    supports_streaming = False
    supports_native_tools = False

    async def chat_completion(self, model, messages, **opts) -> ModelResponse:
        raise NotImplementedError

    async def embed(self, model, texts, *, timeout=120) -> list[list[float]]:
        return [[0.0] * 4 for _ in texts]

    async def list_models(self) -> list[str]:
        return ["embedonly-1"]


# ---------------------------------------------------------------------------
# register / get_provider / available_providers
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_autoload_registers_ollama():
    p = registry.get_provider("ollama")
    assert isinstance(p, OllamaProvider)


@pytest.mark.smoke
def test_register_adds_provider():
    p = _ChatOnlyProvider()
    registry.register("chatonly", p)
    assert registry.get_provider("chatonly") is p
    assert "chatonly" in registry.available_providers()


@pytest.mark.smoke
def test_register_replaces_existing():
    a, b = _ChatOnlyProvider(), _ChatOnlyProvider()
    registry.register("chatonly", a)
    registry.register("chatonly", b)
    assert registry.get_provider("chatonly") is b


@pytest.mark.smoke
def test_register_rejects_empty_name():
    with pytest.raises(ValueError, match="non-empty"):
        registry.register("", _ChatOnlyProvider())


@pytest.mark.smoke
def test_get_provider_unknown_raises_with_listing():
    with pytest.raises(ProviderError, match="unknown provider"):
        registry.get_provider("does-not-exist")


@pytest.mark.smoke
def test_available_providers_is_sorted():
    registry.register("zeta", _ChatOnlyProvider())
    registry.register("alpha", _ChatOnlyProvider())
    names = registry.available_providers()
    assert names == sorted(names)
    assert "alpha" in names and "zeta" in names


# ---------------------------------------------------------------------------
# provider_for_role precedence + capability gate
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_provider_for_role_defaults_to_ollama():
    p = registry.provider_for_role("model_general")
    assert p.name == "ollama"


@pytest.mark.smoke
def test_provider_for_role_overrides_take_precedence():
    registry.register("chatonly", _ChatOnlyProvider())
    p = registry.provider_for_role(
        "model_general", overrides={"model_general_provider": "chatonly"},
    )
    assert p.name == "chatonly"


@pytest.mark.smoke
def test_provider_for_role_settings_override_default(monkeypatch):
    registry.register("chatonly", _ChatOnlyProvider())
    from app.config import settings
    monkeypatch.setattr(settings, "model_verifier_provider", "chatonly")
    p = registry.provider_for_role("model_verifier")
    assert p.name == "chatonly"


@pytest.mark.smoke
def test_provider_for_role_embedder_requires_embeddings():
    registry.register("chatonly", _ChatOnlyProvider())
    with pytest.raises(ProviderCapabilityError, match="embeddings"):
        registry.provider_for_role(
            "model_embedder_pipeline",
            overrides={"model_embedder_pipeline_provider": "chatonly"},
        )


@pytest.mark.smoke
def test_provider_for_role_chat_role_requires_chat():
    registry.register("embedonly", _EmbedOnlyProvider())
    with pytest.raises(ProviderCapabilityError, match="chat"):
        registry.provider_for_role(
            "model_general",
            overrides={"model_general_provider": "embedonly"},
        )


@pytest.mark.smoke
def test_provider_for_role_reranker_exempt_from_chat_gate():
    # Reranker runs as a CrossEncoder singleton outside the provider system,
    # so the chat-capability gate must not fire even on a chat-less provider.
    registry.register("embedonly", _EmbedOnlyProvider())
    p = registry.provider_for_role(
        "model_reranker",
        overrides={"model_reranker_provider": "embedonly"},
    )
    assert p.name == "embedonly"


@pytest.mark.smoke
def test_provider_for_role_unknown_provider_raises():
    with pytest.raises(ProviderError, match="unknown provider"):
        registry.provider_for_role(
            "model_general",
            overrides={"model_general_provider": "no-such-thing"},
        )


# ---------------------------------------------------------------------------
# OllamaProvider delegation — proves the test seam: patching
# model_router._call_ollama still intercepts calls reached through the
# provider abstraction.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ollama_provider_chat_delegates_to_call_ollama():
    p = OllamaProvider()
    fake = AsyncMock(return_value=ModelResponse(
        text="from-mock", model="m", success=True, provider="ollama",
    ))
    from app import model_router
    with patch.object(model_router, "_call_ollama", side_effect=fake):
        resp = await p.chat_completion("m", [{"role": "user", "content": "hi"}])
    assert resp.text == "from-mock"
    assert resp.success is True
    assert fake.await_count >= 1
    # Confirm the endpoint we routed through is /api/chat.
    args, _ = fake.call_args
    assert args[0] == "/api/chat"


@pytest.mark.asyncio
async def test_ollama_provider_generate_delegates_to_call_ollama():
    p = OllamaProvider()
    fake = AsyncMock(return_value=ModelResponse(
        text="from-mock", model="m", success=True, provider="ollama",
    ))
    from app import model_router
    with patch.object(model_router, "_call_ollama", side_effect=fake):
        resp = await p.generate("m", "prompt-text", system="sys")
    assert resp.text == "from-mock"
    args, _ = fake.call_args
    assert args[0] == "/api/generate"
    # System prompt threaded into the payload.
    payload = args[1]
    assert payload["system"] == "sys"
    assert payload["prompt"] == "prompt-text"


@pytest.mark.asyncio
async def test_ollama_provider_embed_delegates_to_call_ollama():
    p = OllamaProvider()
    embedding = [[0.1, 0.2, 0.3]]
    fake = AsyncMock(return_value=ModelResponse(
        text="", model="m", success=True, provider="ollama",
        raw={"embeddings": embedding},
    ))
    from app import model_router
    with patch.object(model_router, "_call_ollama", side_effect=fake):
        result = await p.embed("m", ["hello"])
    assert result == embedding
    args, _ = fake.call_args
    assert args[0] == "/api/embed"


@pytest.mark.asyncio
async def test_ollama_provider_embed_returns_empty_list_on_failure():
    p = OllamaProvider()
    fake = AsyncMock(return_value=ModelResponse(
        model="m", success=False, error="boom", provider="ollama",
    ))
    from app import model_router
    with patch.object(model_router, "_call_ollama", side_effect=fake):
        result = await p.embed("m", ["hello"])
    assert result == []


@pytest.mark.asyncio
async def test_ollama_provider_list_models_delegates():
    p = OllamaProvider()
    from app import model_router
    fake = AsyncMock(return_value=["a", "b"])
    with patch.object(model_router, "list_models", side_effect=fake):
        result = await p.list_models()
    assert result == ["a", "b"]


# ---------------------------------------------------------------------------
# Base class capability defaults — prove the abstract contract surfaces
# clear errors instead of silent None returns.
# ---------------------------------------------------------------------------
class _ChatOnlyConcrete(LLMProvider):
    """Concrete provider that only implements chat_completion + list_models.

    The default LLMProvider.embed / .stream_chat must raise
    ProviderCapabilityError so misconfiguration surfaces a clear error
    instead of silent failure.
    """
    name = "chatonly_concrete"
    supports_chat = True
    supports_embeddings = False

    async def chat_completion(self, model, messages, **opts) -> ModelResponse:
        return ModelResponse(model=model, success=True, provider=self.name)

    async def list_models(self) -> list[str]:
        return []


@pytest.mark.asyncio
async def test_base_embed_default_raises_capability_error():
    p = _ChatOnlyConcrete()
    with pytest.raises(ProviderCapabilityError, match="embeddings"):
        await p.embed("m", ["x"])


@pytest.mark.asyncio
async def test_base_stream_chat_default_raises_capability_error():
    p = _ChatOnlyConcrete()
    # stream_chat is an async generator — capability error must fire on entry,
    # not silently yield nothing.
    with pytest.raises(ProviderCapabilityError, match="streaming"):
        agen = p.stream_chat("m", [{"role": "user", "content": "x"}])
        await agen.__anext__()


@pytest.mark.asyncio
async def test_base_health_check_reports_up_when_list_models_works():
    p = _ChatOnlyConcrete()
    health = await p.health_check()
    assert health["status"] == "up"
    assert health["provider"] == "chatonly_concrete"
    assert "latency_ms" in health


@pytest.mark.asyncio
async def test_base_health_check_reports_down_on_error():
    class _Broken(_ChatOnlyConcrete):
        async def list_models(self) -> list[str]:
            raise RuntimeError("nope")
    p = _Broken()
    health = await p.health_check()
    assert health["status"] == "down"
    assert "nope" in health["error"]
