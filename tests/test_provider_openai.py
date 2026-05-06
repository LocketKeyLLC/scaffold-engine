"""Tests for app/providers/openai.py — Sprint F.

Mocks the shared httpx client so no live HTTP traffic is generated. All
upstream-shape parsing (chat-completion, embedding, /models list) is asserted
against fixtures that mirror the real OpenAI response envelopes.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app import providers as registry
from app.providers.base import (
    ProviderCapabilityError,
    ProviderUnavailableError,
)
from app.providers.openai import OpenAIProvider


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _resp(status: int, payload: dict | None = None, text: str = ""):
    """Fake httpx.Response."""
    r = MagicMock()
    r.status_code = status
    r.text = text
    r.json = MagicMock(return_value=payload or {})
    return r


@pytest.fixture
def with_api_key():
    """Set a fake API key for the duration of the test."""
    from app.config import settings
    from pydantic import SecretStr
    saved = settings.openai_api_key
    settings.openai_api_key = SecretStr("sk-test-fake")
    try:
        yield
    finally:
        settings.openai_api_key = saved


@pytest.fixture
def fake_client(with_api_key):
    """Patch OpenAIProvider._client() to return an AsyncMock."""
    client = AsyncMock()
    with patch.object(OpenAIProvider, "_client", staticmethod(lambda: client)):
        yield client


# ---------------------------------------------------------------------------
# Capability flags + registry
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_openai_provider_capability_flags():
    p = OpenAIProvider()
    assert p.name == "openai"
    assert p.supports_chat is True
    assert p.supports_embeddings is True
    assert p.supports_streaming is True
    assert p.supports_native_tools is True


@pytest.mark.smoke
def test_autoload_registers_openai():
    p = registry.get_provider("openai")
    assert isinstance(p, OpenAIProvider)


@pytest.mark.smoke
def test_provider_for_role_routes_to_openai_when_configured():
    """provider_for_role honors a runtime override naming "openai"."""
    p = registry.provider_for_role(
        "model_general",
        overrides={"model_general_provider": "openai"},
    )
    assert p.name == "openai"


# ---------------------------------------------------------------------------
# Auth — empty key surfaces a clear, actionable error
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_auth_headers_raise_when_key_empty():
    """Empty OPENAI_API_KEY must raise ProviderUnavailableError with a
    remediation hint, not a silent 401 from upstream."""
    from app.config import settings
    from pydantic import SecretStr
    saved = settings.openai_api_key
    settings.openai_api_key = SecretStr("")
    try:
        with pytest.raises(ProviderUnavailableError, match="OPENAI_API_KEY"):
            OpenAIProvider._auth_headers()
    finally:
        settings.openai_api_key = saved


@pytest.mark.asyncio
async def test_chat_completion_returns_failure_when_key_empty():
    """chat_completion must return ModelResponse(success=False) — never
    raise — so callers that don't expect provider exceptions stay sane."""
    from app.config import settings
    from pydantic import SecretStr
    saved = settings.openai_api_key
    settings.openai_api_key = SecretStr("")
    try:
        p = OpenAIProvider()
        resp = await p.chat_completion("gpt-4o-mini", [{"role": "user", "content": "hi"}])
        assert resp.success is False
        assert "OPENAI_API_KEY" in resp.error
        assert resp.provider == "openai"
    finally:
        settings.openai_api_key = saved


@pytest.mark.asyncio
async def test_embed_returns_empty_list_when_key_empty():
    """Empty key on embed must yield [] (matching the contract: embed never
    raises) — and log a clear error so ops can spot it."""
    from app.config import settings
    from pydantic import SecretStr
    saved = settings.openai_api_key
    settings.openai_api_key = SecretStr("")
    try:
        p = OpenAIProvider()
        result = await p.embed("text-embedding-3-small", ["hi"])
        assert result == []
    finally:
        settings.openai_api_key = saved


# ---------------------------------------------------------------------------
# chat_completion — happy path + error parsing
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_chat_completion_parses_openai_envelope(fake_client):
    fake_client.post.return_value = _resp(200, {
        "id": "chatcmpl-1",
        "model": "gpt-4o-mini",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "hello there"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    })
    p = OpenAIProvider()
    resp = await p.chat_completion(
        "gpt-4o-mini",
        [{"role": "user", "content": "hi"}],
    )
    assert resp.success is True
    assert resp.text == "hello there"
    assert resp.model == "gpt-4o-mini"
    assert resp.tokens_prompt == 5
    assert resp.tokens_completion == 3
    assert resp.provider == "openai"

    # Confirm the request shape — endpoint, auth header, payload
    args, kwargs = fake_client.post.call_args
    assert args[0] == "/chat/completions"
    payload = kwargs["json"]
    assert payload["model"] == "gpt-4o-mini"
    assert payload["stream"] is False
    headers = kwargs["headers"]
    assert headers["Authorization"] == "Bearer sk-test-fake"


@pytest.mark.asyncio
async def test_chat_completion_extracts_openai_error_message(fake_client):
    """OpenAI returns errors in {error: {message, type, code}}; we surface
    the .message verbatim instead of a generic HTTP code."""
    fake_client.post.return_value = _resp(
        400,
        {"error": {"message": "model `gpt-9` not found", "type": "invalid_request"}},
    )
    p = OpenAIProvider()
    resp = await p.chat_completion("gpt-9", [{"role": "user", "content": "x"}])
    assert resp.success is False
    assert "model `gpt-9` not found" in resp.error
    assert "HTTP 400" in resp.error
    assert resp.provider == "openai"


@pytest.mark.asyncio
async def test_chat_completion_handles_timeout(fake_client):
    fake_client.post.side_effect = httpx.TimeoutException("boom")
    p = OpenAIProvider()
    resp = await p.chat_completion(
        "gpt-4o-mini", [{"role": "user", "content": "x"}], timeout=5,
    )
    assert resp.success is False
    assert "Timeout" in resp.error
    assert resp.provider == "openai"


@pytest.mark.asyncio
async def test_chat_completion_handles_unexpected_exception(fake_client):
    fake_client.post.side_effect = RuntimeError("network unplugged")
    p = OpenAIProvider()
    resp = await p.chat_completion("gpt-4o-mini", [{"role": "user", "content": "x"}])
    assert resp.success is False
    assert "network unplugged" in resp.error
    assert resp.provider == "openai"


@pytest.mark.asyncio
async def test_chat_completion_threads_extra_opts_to_payload(fake_client):
    """Caller extras (response_format, top_p, ...) must propagate to the
    payload, but Ollama-only opts (fallback) get filtered out."""
    fake_client.post.return_value = _resp(200, {
        "model": "gpt-4o-mini",
        "choices": [{"message": {"content": "x"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    })
    p = OpenAIProvider()
    await p.chat_completion(
        "gpt-4o-mini",
        [{"role": "user", "content": "x"}],
        response_format={"type": "json_object"},
        top_p=0.9,
        fallback="ignored",  # Ollama-specific; must not appear in OpenAI payload
    )
    payload = fake_client.post.call_args.kwargs["json"]
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["top_p"] == 0.9
    assert "fallback" not in payload


# ---------------------------------------------------------------------------
# embed — happy path + error handling + index sort
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_embed_parses_response_and_sets_dimensions(fake_client):
    """embed must request settings.embedding_dim and return one vector per
    input in input order (sorted by index defensively)."""
    fake_client.post.return_value = _resp(200, {
        "object": "list",
        "data": [
            # Out of order to verify the index-sort
            {"object": "embedding", "index": 1, "embedding": [0.4, 0.5, 0.6]},
            {"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]},
        ],
        "model": "text-embedding-3-small",
        "usage": {"prompt_tokens": 4, "total_tokens": 4},
    })
    p = OpenAIProvider()
    result = await p.embed("text-embedding-3-small", ["a", "b"])
    assert result == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    payload = fake_client.post.call_args.kwargs["json"]
    from app.config import settings
    assert payload["dimensions"] == settings.embedding_dim
    assert payload["input"] == ["a", "b"]


@pytest.mark.asyncio
async def test_embed_returns_empty_on_http_error(fake_client):
    fake_client.post.return_value = _resp(429, {
        "error": {"message": "rate limit exceeded"}
    })
    p = OpenAIProvider()
    result = await p.embed("text-embedding-3-small", ["a"])
    assert result == []


@pytest.mark.asyncio
async def test_embed_returns_empty_on_timeout(fake_client):
    fake_client.post.side_effect = httpx.TimeoutException("boom")
    p = OpenAIProvider()
    result = await p.embed("text-embedding-3-small", ["a"], timeout=5)
    assert result == []


# ---------------------------------------------------------------------------
# list_models
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_list_models_returns_ids(fake_client):
    fake_client.get.return_value = _resp(200, {
        "object": "list",
        "data": [
            {"id": "gpt-4o-mini", "object": "model"},
            {"id": "gpt-4o", "object": "model"},
            {"id": "text-embedding-3-small", "object": "model"},
            {"id": "", "object": "model"},  # Empty id should be filtered
        ],
    })
    p = OpenAIProvider()
    result = await p.list_models()
    assert result == ["gpt-4o-mini", "gpt-4o", "text-embedding-3-small"]


@pytest.mark.asyncio
async def test_list_models_returns_empty_on_error(fake_client):
    fake_client.get.return_value = _resp(401, {
        "error": {"message": "invalid api key"}
    })
    p = OpenAIProvider()
    result = await p.list_models()
    assert result == []


@pytest.mark.asyncio
async def test_list_models_returns_empty_when_key_empty():
    """list_models is used by health_check; must NOT raise on missing key —
    instead return [] so health probes report a clear 'down' status."""
    from app.config import settings
    from pydantic import SecretStr
    saved = settings.openai_api_key
    settings.openai_api_key = SecretStr("")
    try:
        p = OpenAIProvider()
        result = await p.list_models()
        assert result == []
    finally:
        settings.openai_api_key = saved


# ---------------------------------------------------------------------------
# Streaming + tool-calls — advertised but impl deferred (Sprints I+)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_stream_chat_raises_capability_error_until_implemented():
    """The base-class default raises ProviderCapabilityError on stream_chat.
    OpenAIProvider doesn't override it yet (deferred to streaming-uniformity
    sprint). When implementation lands, this test should be replaced with a
    real streaming test."""
    p = OpenAIProvider()
    with pytest.raises(ProviderCapabilityError, match="streaming"):
        agen = p.stream_chat("gpt-4o-mini", [{"role": "user", "content": "x"}])
        await agen.__anext__()
