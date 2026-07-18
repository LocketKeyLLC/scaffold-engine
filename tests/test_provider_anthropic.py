"""Tests for app/providers/anthropic.py — §17.345.

Mocks the shared httpx client so no live HTTP traffic is generated. All
upstream-shape parsing (Messages API response, tool_use blocks, SSE stream,
prompt caching, Opus 4.7 sampling-param stripping) is asserted against
fixtures that mirror the real Anthropic response envelopes.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import providers as registry
from app.providers.anthropic import AnthropicProvider
from app.providers.base import (
    ProviderCapabilityError,
    ProviderUnavailableError,
    Tool,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _resp(status: int, payload: dict | None = None, text: str = ""):
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
    saved = settings.anthropic_api_key
    settings.anthropic_api_key = SecretStr("sk-ant-fake")
    try:
        yield
    finally:
        settings.anthropic_api_key = saved


@pytest.fixture
def fake_client(with_api_key):
    """Patch AnthropicProvider._client() to return an AsyncMock."""
    client = AsyncMock()
    with patch.object(AnthropicProvider, "_client", staticmethod(lambda: client)):
        yield client


# ---------------------------------------------------------------------------
# Capability flags + registry
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_anthropic_provider_capability_flags():
    p = AnthropicProvider()
    assert p.name == "anthropic"
    assert p.supports_chat is True
    assert p.supports_embeddings is False  # Anthropic has no embeddings API
    assert p.supports_streaming is True
    assert p.supports_native_tools is True


@pytest.mark.smoke
def test_autoload_registers_anthropic():
    p = registry.get_provider("anthropic")
    assert isinstance(p, AnthropicProvider)


@pytest.mark.smoke
def test_provider_for_role_routes_to_anthropic_when_configured():
    p = registry.provider_for_role(
        "model_general",
        overrides={"model_general_provider": "anthropic"},
    )
    assert p.name == "anthropic"


@pytest.mark.smoke
def test_provider_for_role_rejects_anthropic_on_embedder_role():
    """Capability gate fires at config-resolve time — embedder role bound
    to anthropic must raise, not silently accept and crash mid-pipeline."""
    with pytest.raises(ProviderCapabilityError, match="embeddings"):
        registry.provider_for_role(
            "model_embedder_pipeline",
            overrides={"model_embedder_pipeline_provider": "anthropic"},
        )


# ---------------------------------------------------------------------------
# Auth — empty key surfaces a clear, actionable error
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_auth_headers_raise_when_key_empty():
    from app.config import settings
    from pydantic import SecretStr
    saved = settings.anthropic_api_key
    settings.anthropic_api_key = SecretStr("")
    try:
        with pytest.raises(ProviderUnavailableError, match="ANTHROPIC_API_KEY"):
            AnthropicProvider._auth_headers()
    finally:
        settings.anthropic_api_key = saved


@pytest.mark.smoke
def test_auth_headers_include_version(with_api_key):
    """anthropic-version header is REQUIRED by the API — verify it's set."""
    headers = AnthropicProvider._auth_headers()
    assert "anthropic-version" in headers
    assert headers["x-api-key"] == "sk-ant-fake"
    # Must NOT use Bearer scheme (that's OpenAI's convention).
    assert "Authorization" not in headers


@pytest.mark.asyncio
async def test_chat_completion_returns_failure_when_key_empty():
    """chat_completion must return ModelResponse(success=False), never raise."""
    from app.config import settings
    from pydantic import SecretStr
    saved = settings.anthropic_api_key
    settings.anthropic_api_key = SecretStr("")
    try:
        p = AnthropicProvider()
        resp = await p.chat_completion("claude-opus-4-7", [{"role": "user", "content": "hi"}])
        assert resp.success is False
        assert "ANTHROPIC_API_KEY" in resp.error
        assert resp.provider == "anthropic"
    finally:
        settings.anthropic_api_key = saved


# ---------------------------------------------------------------------------
# Payload shape — the bits that differ from OpenAI
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_chat_completion_splits_system_to_top_level(fake_client):
    """Anthropic puts system top-level, NOT in the messages array."""
    fake_client.post = AsyncMock(return_value=_resp(200, {
        "id": "msg_01", "model": "claude-sonnet-4-6",
        "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 5, "output_tokens": 2},
    }))
    p = AnthropicProvider()
    await p.chat_completion(
        "claude-sonnet-4-6",
        [
            {"role": "system", "content": "You are concise."},
            {"role": "user", "content": "hi"},
        ],
    )
    sent = fake_client.post.call_args.kwargs["json"]
    # system promoted top-level
    assert "system" in sent
    # messages contains only user/assistant
    assert all(m["role"] in ("user", "assistant") for m in sent["messages"])
    assert len(sent["messages"]) == 1


@pytest.mark.asyncio
async def test_chat_completion_strips_temperature_for_opus_4_7(fake_client):
    """Opus 4.7 rejects temperature/top_p/top_k — provider must strip them."""
    fake_client.post = AsyncMock(return_value=_resp(200, {
        "id": "msg_01", "model": "claude-opus-4-7",
        "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 5, "output_tokens": 2},
    }))
    p = AnthropicProvider()
    await p.chat_completion(
        "claude-opus-4-7",
        [{"role": "user", "content": "hi"}],
        temperature=0.7,
    )
    sent = fake_client.post.call_args.kwargs["json"]
    assert "temperature" not in sent, (
        "temperature must be stripped for Opus 4.7 (returns 400 if sent)"
    )


@pytest.mark.asyncio
async def test_chat_completion_passes_temperature_for_sonnet(fake_client):
    """Sonnet 4.6 accepts temperature — provider must pass it through."""
    fake_client.post = AsyncMock(return_value=_resp(200, {
        "id": "msg_01", "model": "claude-sonnet-4-6",
        "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 5, "output_tokens": 2},
    }))
    p = AnthropicProvider()
    await p.chat_completion(
        "claude-sonnet-4-6",
        [{"role": "user", "content": "hi"}],
        temperature=0.3,
    )
    sent = fake_client.post.call_args.kwargs["json"]
    assert sent["temperature"] == 0.3


@pytest.mark.asyncio
async def test_chat_completion_applies_prompt_caching_when_enabled(fake_client):
    """When caching is on (default) and system is present, the system block
    must carry cache_control. This is the §17.345 high-volume-routing rationale."""
    from app.config import settings
    assert settings.anthropic_prompt_caching is True, "default must be on"
    fake_client.post = AsyncMock(return_value=_resp(200, {
        "id": "msg_01", "model": "claude-sonnet-4-6",
        "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 5, "output_tokens": 2},
    }))
    p = AnthropicProvider()
    await p.chat_completion(
        "claude-sonnet-4-6",
        [
            {"role": "system", "content": "Long stable system prompt."},
            {"role": "user", "content": "hi"},
        ],
    )
    sent = fake_client.post.call_args.kwargs["json"]
    assert isinstance(sent["system"], list)
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_chat_completion_skips_caching_when_disabled(fake_client):
    """anthropic_prompt_caching=False → system as a plain string, no marker."""
    from app.config import settings
    saved = settings.anthropic_prompt_caching
    settings.anthropic_prompt_caching = False
    try:
        fake_client.post = AsyncMock(return_value=_resp(200, {
            "id": "msg_01", "model": "claude-sonnet-4-6",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 5, "output_tokens": 2},
        }))
        p = AnthropicProvider()
        await p.chat_completion(
            "claude-sonnet-4-6",
            [
                {"role": "system", "content": "Plain system."},
                {"role": "user", "content": "hi"},
            ],
        )
        sent = fake_client.post.call_args.kwargs["json"]
        assert isinstance(sent["system"], str)
        assert sent["system"] == "Plain system."
    finally:
        settings.anthropic_prompt_caching = saved


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_chat_completion_extracts_text_and_usage(fake_client):
    fake_client.post = AsyncMock(return_value=_resp(200, {
        "id": "msg_01", "model": "claude-sonnet-4-6",
        "content": [
            {"type": "text", "text": "Hello "},
            {"type": "text", "text": "world"},
        ],
        "usage": {
            "input_tokens": 10, "output_tokens": 5,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 200,
        },
    }))
    p = AnthropicProvider()
    resp = await p.chat_completion("claude-sonnet-4-6", [{"role": "user", "content": "hi"}])
    assert resp.success is True
    # Multi-text-block concatenation
    assert resp.text == "Hello world"
    assert resp.tokens_completion == 5
    # tokens_prompt includes cache reads (cost is downstream's problem)
    assert resp.tokens_prompt == 210
    assert resp.provider == "anthropic"
    # Raw payload preserved for downstream cost-tracking detail
    assert resp.raw["usage"]["cache_read_input_tokens"] == 200


@pytest.mark.asyncio
async def test_chat_completion_returns_failure_on_http_error(fake_client):
    fake_client.post = AsyncMock(return_value=_resp(400, {
        "type": "error",
        "error": {"type": "invalid_request_error", "message": "bad request"},
    }))
    p = AnthropicProvider()
    resp = await p.chat_completion("claude-sonnet-4-6", [{"role": "user", "content": "hi"}])
    assert resp.success is False
    assert "bad request" in resp.error
    assert "400" in resp.error


# ---------------------------------------------------------------------------
# embed — capability gate must fire
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_embed_raises_capability_error():
    p = AnthropicProvider()
    with pytest.raises(ProviderCapabilityError, match="embeddings"):
        await p.embed("claude-sonnet-4-6", ["hello"])


# ---------------------------------------------------------------------------
# Tool calls — wire shape and parsing
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_tool_call_emits_anthropic_wire_shape(fake_client):
    """Anthropic tool shape is flat (name/description/input_schema), NOT
    wrapped in {type: function, function: {...}} like OpenAI."""
    fake_client.post = AsyncMock(return_value=_resp(200, {
        "id": "msg_01", "model": "claude-sonnet-4-6",
        "stop_reason": "tool_use",
        "content": [{
            "type": "tool_use", "id": "toolu_01",
            "name": "get_weather", "input": {"location": "Paris"},
        }],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }))
    p = AnthropicProvider()
    tools = [Tool(
        name="get_weather",
        description="Get weather",
        input_schema={"type": "object", "properties": {"location": {"type": "string"}}},
    )]
    resp = await p.tool_call(
        "claude-sonnet-4-6",
        [{"role": "user", "content": "weather in Paris"}],
        tools, tool_choice="auto",
    )
    sent = fake_client.post.call_args.kwargs["json"]
    # No function-wrapping; flat tool objects.
    assert sent["tools"][0]["name"] == "get_weather"
    assert "function" not in sent["tools"][0]
    assert sent["tools"][0]["input_schema"] == tools[0].input_schema
    # tool_choice translated to typed object
    assert sent["tool_choice"] == {"type": "auto"}

    # Response parsed into ToolCall (input is already a dict, no JSON-string decode)
    assert resp.success is True
    assert len(resp.tool_calls) == 1
    tc = resp.tool_calls[0]
    assert tc.id == "toolu_01"
    assert tc.name == "get_weather"
    assert tc.arguments == {"location": "Paris"}


@pytest.mark.asyncio
async def test_tool_call_choice_required_maps_to_any(fake_client):
    """OpenAI's 'required' = Anthropic's 'any' — provider must translate."""
    fake_client.post = AsyncMock(return_value=_resp(200, {
        "id": "msg_01", "model": "claude-sonnet-4-6",
        "content": [], "usage": {"input_tokens": 5, "output_tokens": 0},
    }))
    p = AnthropicProvider()
    await p.tool_call(
        "claude-sonnet-4-6",
        [{"role": "user", "content": "x"}],
        [Tool(name="t", description="d", input_schema={"type": "object"})],
        tool_choice="required",
    )
    sent = fake_client.post.call_args.kwargs["json"]
    assert sent["tool_choice"] == {"type": "any"}


@pytest.mark.asyncio
async def test_tool_call_choice_specific_tool_name(fake_client):
    fake_client.post = AsyncMock(return_value=_resp(200, {
        "id": "msg_01", "model": "claude-sonnet-4-6",
        "content": [], "usage": {"input_tokens": 5, "output_tokens": 0},
    }))
    p = AnthropicProvider()
    await p.tool_call(
        "claude-sonnet-4-6",
        [{"role": "user", "content": "x"}],
        [Tool(name="get_weather", description="d", input_schema={"type": "object"})],
        tool_choice="get_weather",
    )
    sent = fake_client.post.call_args.kwargs["json"]
    assert sent["tool_choice"] == {"type": "tool", "name": "get_weather"}


# ---------------------------------------------------------------------------
# list_models — fail-soft on errors
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_list_models_fail_soft_on_empty_key():
    """No key → empty list (not an exception). Mirrors OpenAIProvider."""
    from app.config import settings
    from pydantic import SecretStr
    saved = settings.anthropic_api_key
    settings.anthropic_api_key = SecretStr("")
    try:
        p = AnthropicProvider()
        out = await p.list_models()
        assert out == []
    finally:
        settings.anthropic_api_key = saved


@pytest.mark.asyncio
async def test_list_models_returns_ids(fake_client):
    fake_client.get = AsyncMock(return_value=_resp(200, {
        "data": [
            {"id": "claude-opus-4-7", "type": "model"},
            {"id": "claude-sonnet-4-6", "type": "model"},
        ],
    }))
    p = AnthropicProvider()
    out = await p.list_models()
    assert out == ["claude-opus-4-7", "claude-sonnet-4-6"]


# ---------------------------------------------------------------------------
# §17.610 (audit #38) — mid-stream error frames must propagate
# ---------------------------------------------------------------------------
class _FakeStreamResp:
    def __init__(self, status_code=200, lines=None):
        self.status_code = status_code
        self._lines = lines or []

    async def aread(self):
        return b""

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeStreamCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_stream_chat_raises_on_midstream_error_after_partial(fake_client):
    """A mid-stream {type:'error', overloaded_error} frame on a 200 stream must
    raise ProviderUnavailableError — NOT be silently swallowed so a consumer
    accepts truncated partial content as a complete response."""
    lines = [
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hel"}}',
        'data: {"type":"error","error":{"type":"overloaded_error","message":"overloaded"}}',
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"lo"}}',
    ]
    fake_client.stream = lambda *a, **k: _FakeStreamCtx(_FakeStreamResp(200, lines))

    p = AnthropicProvider()
    collected = []
    with pytest.raises(ProviderUnavailableError, match="overloaded"):
        async for chunk in p.stream_chat("claude-opus-4-7", [{"role": "user", "content": "x"}]):
            collected.append(chunk)
    # The partial content before the error was yielded; the post-error delta was not.
    assert collected == ["Hel"]
