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
    Tool,
    ToolCall,
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
# Streaming — Sprint I.1 — SSE parsing + terminator + error paths
# ---------------------------------------------------------------------------
class _FakeStreamResp:
    def __init__(self, status_code=200, lines=None, body=b""):
        self.status_code = status_code
        self._lines = lines or []
        self._body = body

    async def aread(self):
        return self._body

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


def _fake_stream(status_code=200, lines=None, body=b""):
    """Build a function-mock for ``client.stream`` that returns a context
    manager yielding our fake response."""
    def _factory(*args, **kwargs):
        return _FakeStreamCtx(_FakeStreamResp(status_code, lines, body))
    return _factory


@pytest.mark.asyncio
async def test_stream_chat_yields_content_deltas(fake_client):
    fake_client.stream = _fake_stream(lines=[
        'data: {"choices": [{"delta": {"role": "assistant"}}]}',
        'data: {"choices": [{"delta": {"content": "hello"}}]}',
        'data: {"choices": [{"delta": {"content": " world"}}]}',
        'data: [DONE]',
    ])
    p = OpenAIProvider()
    chunks: list[str] = []
    async for chunk in p.stream_chat("gpt-4o-mini", [{"role": "user", "content": "hi"}]):
        chunks.append(chunk)
    assert chunks == ["hello", " world"]


@pytest.mark.asyncio
async def test_stream_chat_stops_at_done_terminator(fake_client):
    """Anything after `data: [DONE]` must NOT be yielded — the stream
    is over the moment the terminator arrives."""
    fake_client.stream = _fake_stream(lines=[
        'data: {"choices": [{"delta": {"content": "yes"}}]}',
        'data: [DONE]',
        'data: {"choices": [{"delta": {"content": "should-not-appear"}}]}',
    ])
    p = OpenAIProvider()
    chunks = [c async for c in p.stream_chat("m", [{"role": "user", "content": "x"}])]
    assert chunks == ["yes"]


@pytest.mark.asyncio
async def test_stream_chat_skips_non_data_lines(fake_client):
    """OpenAI sometimes emits ``event:`` headers and blank lines in SSE.
    The stream parser must ignore anything that isn't a data frame."""
    fake_client.stream = _fake_stream(lines=[
        "",
        "event: ping",
        "",
        'data: {"choices": [{"delta": {"content": "ok"}}]}',
        "",
        'data: [DONE]',
    ])
    p = OpenAIProvider()
    chunks = [c async for c in p.stream_chat("m", [{"role": "user", "content": "x"}])]
    assert chunks == ["ok"]


@pytest.mark.asyncio
async def test_stream_chat_skips_chunks_with_empty_or_missing_content(fake_client):
    """Some SSE chunks carry only role/finish_reason metadata with no
    delta.content — those must not produce empty string yields."""
    fake_client.stream = _fake_stream(lines=[
        'data: {"choices": [{"delta": {"role": "assistant"}}]}',
        'data: {"choices": [{"delta": {"content": ""}}]}',
        'data: {"choices": []}',
        'data: {"choices": [{"delta": {"content": "real"}}]}',
        'data: [DONE]',
    ])
    p = OpenAIProvider()
    chunks = [c async for c in p.stream_chat("m", [{"role": "user", "content": "x"}])]
    assert chunks == ["real"]


@pytest.mark.asyncio
async def test_stream_chat_raises_provider_unavailable_on_non_200(fake_client):
    fake_client.stream = _fake_stream(
        status_code=401,
        body=b'{"error":{"message":"invalid api key"}}',
    )
    p = OpenAIProvider()
    with pytest.raises(ProviderUnavailableError, match="openai HTTP 401"):
        agen = p.stream_chat("m", [{"role": "user", "content": "x"}])
        await agen.__anext__()


@pytest.mark.asyncio
async def test_stream_chat_raises_when_key_empty():
    """Missing OPENAI_API_KEY must surface as ProviderUnavailableError on
    the first await — same contract as chat_completion."""
    from app.config import settings
    from pydantic import SecretStr
    saved = settings.openai_api_key
    settings.openai_api_key = SecretStr("")
    try:
        p = OpenAIProvider()
        with pytest.raises(ProviderUnavailableError, match="OPENAI_API_KEY"):
            agen = p.stream_chat("m", [{"role": "user", "content": "x"}])
            await agen.__anext__()
    finally:
        settings.openai_api_key = saved


@pytest.mark.asyncio
async def test_stream_chat_payload_marks_stream_true(fake_client):
    """The wire payload must set stream=True so the OpenAI server emits
    the SSE shape (a missing flag silently downgrades to a single JSON
    response)."""
    captured: dict = {}

    def _capture(*args, **kwargs):
        captured.update(kwargs)
        return _FakeStreamCtx(_FakeStreamResp(200, lines=['data: [DONE]']))

    fake_client.stream = _capture
    p = OpenAIProvider()
    async for _ in p.stream_chat("m", [{"role": "user", "content": "x"}]):
        pass
    assert captured["json"]["stream"] is True
    assert captured["headers"]["Authorization"] == "Bearer sk-test-fake"


# ---------------------------------------------------------------------------
# Sprint I.2 — tool_call (POST /chat/completions with tools=[...])
# ---------------------------------------------------------------------------
_TOOL_SEARCH = Tool(
    name="search_web",
    description="Search the web for recent results",
    input_schema={
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
)


@pytest.mark.asyncio
async def test_tool_call_translates_tools_to_openai_wire_shape(fake_client):
    """OpenAI expects tools wrapped as {type: function, function: {name,
    description, parameters}}. The translator must produce exactly that."""
    fake_client.post.return_value = _resp(200, {
        "model": "gpt-4o-mini",
        "choices": [{
            "message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {
                    "name": "search_web",
                    "arguments": '{"query": "rag"}',
                }},
            ]},
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    })
    p = OpenAIProvider()
    await p.tool_call("gpt-4o-mini",
                      [{"role": "user", "content": "search RAG"}],
                      [_TOOL_SEARCH])
    payload = fake_client.post.call_args.kwargs["json"]
    assert payload["tools"] == [{
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Search the web for recent results",
            "parameters": _TOOL_SEARCH.input_schema,
        },
    }]
    assert payload["tool_choice"] == "auto"
    assert payload["stream"] is False


@pytest.mark.asyncio
async def test_tool_call_decodes_arguments_json_string(fake_client):
    """OpenAI emits function.arguments as a JSON-encoded STRING. The
    parser must decode it so callers receive a structured dict, not a
    string they have to json.loads themselves."""
    fake_client.post.return_value = _resp(200, {
        "model": "gpt-4o-mini",
        "choices": [{
            "message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_abc", "type": "function", "function": {
                    "name": "search_web",
                    "arguments": '{"query": "transformers", "limit": 5}',
                }},
            ]},
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    })
    p = OpenAIProvider()
    resp = await p.tool_call("gpt-4o-mini",
                             [{"role": "user", "content": "x"}],
                             [_TOOL_SEARCH])
    assert resp.success is True
    assert len(resp.tool_calls) == 1
    tc = resp.tool_calls[0]
    assert tc.id == "call_abc"
    assert tc.name == "search_web"
    assert tc.arguments == {"query": "transformers", "limit": 5}


@pytest.mark.asyncio
async def test_tool_call_malformed_arguments_become_empty_dict(fake_client):
    """If the model emits invalid JSON in arguments (rare with constrained
    decoding, possible with weaker fine-tunes), we shouldn't crash the
    whole call — yield an empty dict and let the caller decide."""
    fake_client.post.return_value = _resp(200, {
        "model": "gpt-4o-mini",
        "choices": [{
            "message": {"content": None, "tool_calls": [
                {"id": "call_x", "function": {
                    "name": "search_web",
                    "arguments": "this is not json {",
                }},
            ]},
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    })
    p = OpenAIProvider()
    resp = await p.tool_call("gpt-4o-mini",
                             [{"role": "user", "content": "x"}],
                             [_TOOL_SEARCH])
    assert resp.success is True
    assert resp.tool_calls[0].arguments == {}


@pytest.mark.asyncio
async def test_tool_call_text_only_response_has_empty_tool_calls(fake_client):
    """Model with tool_choice=auto can decide to respond in text — the
    response must reflect that with text populated and tool_calls empty."""
    fake_client.post.return_value = _resp(200, {
        "model": "gpt-4o-mini",
        "choices": [{
            "message": {"role": "assistant",
                        "content": "I'll just describe instead."},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 5},
    })
    p = OpenAIProvider()
    resp = await p.tool_call("gpt-4o-mini",
                             [{"role": "user", "content": "x"}],
                             [_TOOL_SEARCH])
    assert resp.success is True
    assert resp.text == "I'll just describe instead."
    assert resp.tool_calls == []


@pytest.mark.asyncio
async def test_tool_choice_specific_name_wraps_in_function_object(fake_client):
    """OpenAI accepts ``"auto"``/``"none"``/``"required"`` as bare strings
    but a specific tool name must be wrapped as
    {"type": "function", "function": {"name": "<tool_name>"}}."""
    fake_client.post.return_value = _resp(200, {
        "model": "gpt-4o-mini",
        "choices": [{"message": {"content": ""}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    })
    p = OpenAIProvider()
    await p.tool_call("gpt-4o-mini",
                      [{"role": "user", "content": "x"}],
                      [_TOOL_SEARCH],
                      tool_choice="search_web")
    payload = fake_client.post.call_args.kwargs["json"]
    assert payload["tool_choice"] == {
        "type": "function",
        "function": {"name": "search_web"},
    }


@pytest.mark.asyncio
async def test_tool_choice_special_strings_pass_through(fake_client):
    """``auto``, ``none``, and ``required`` are passed through as-is."""
    fake_client.post.return_value = _resp(200, {
        "model": "gpt-4o-mini",
        "choices": [{"message": {"content": ""}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    })
    p = OpenAIProvider()
    for choice in ("auto", "none", "required"):
        await p.tool_call("gpt-4o-mini",
                          [{"role": "user", "content": "x"}],
                          [_TOOL_SEARCH],
                          tool_choice=choice)
        assert fake_client.post.call_args.kwargs["json"]["tool_choice"] == choice


@pytest.mark.asyncio
async def test_tool_call_returns_failure_when_key_empty():
    """Same contract as chat_completion — empty key surfaces as a
    structured failure response, not an exception."""
    from app.config import settings
    from pydantic import SecretStr
    saved = settings.openai_api_key
    settings.openai_api_key = SecretStr("")
    try:
        p = OpenAIProvider()
        resp = await p.tool_call(
            "gpt-4o-mini", [{"role": "user", "content": "x"}], [_TOOL_SEARCH],
        )
        assert resp.success is False
        assert "OPENAI_API_KEY" in resp.error
        assert resp.tool_calls == []
    finally:
        settings.openai_api_key = saved


@pytest.mark.asyncio
async def test_tool_call_extracts_openai_error_on_non_200(fake_client):
    fake_client.post.return_value = _resp(
        429, {"error": {"message": "rate_limit_exceeded"}}
    )
    p = OpenAIProvider()
    resp = await p.tool_call(
        "gpt-4o-mini", [{"role": "user", "content": "x"}], [_TOOL_SEARCH],
    )
    assert resp.success is False
    assert "rate_limit_exceeded" in resp.error
    assert resp.tool_calls == []
