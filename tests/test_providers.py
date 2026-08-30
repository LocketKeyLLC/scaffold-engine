"""Tests for app/providers/ — registry, capability gate, autoload.

Sprint E. The registry is module-level state so each test snapshots
``_PROVIDERS`` and restores it on teardown to keep tests isolated.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import providers as registry
from app.providers.base import (
    LLMProvider,
    ModelResponse,
    ProviderCapabilityError,
    ProviderError,
    ProviderUnavailableError,
    Tool,
    ToolCall,
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
    # §17.683 — no think key by default (leaves the model's own default).
    assert "think" not in payload


@pytest.mark.asyncio
async def test_ollama_provider_generate_think_false_threads_into_payload():
    """§17.683 — think=False must reach the /api/generate payload so a reasoning
    model sends its whole num_predict budget to the answer (DAG-gen fix)."""
    p = OllamaProvider()
    fake = AsyncMock(return_value=ModelResponse(
        text="{}", model="m", success=True, provider="ollama",
    ))
    from app import model_router
    with patch.object(model_router, "_call_ollama", side_effect=fake):
        await p.generate("m", "prompt-text", think=False)
    payload = fake.call_args[0][1]
    assert payload["think"] is False


@pytest.mark.asyncio
async def test_model_router_generate_think_threads_through_role_path():
    """§17.683 — model_router.generate(role=..., think=False) forwards think to
    the provider (the DAG generator relies on this end-to-end)."""
    from app import model_router
    captured = {}

    async def _fake_generate(model, prompt, **kwargs):
        captured.update(kwargs)
        return ModelResponse(text="{}", model=model, success=True, provider="ollama")

    fake_provider = MagicMock()
    fake_provider.generate = AsyncMock(side_effect=_fake_generate)
    with patch.object(model_router, "_resolve_role",
                      return_value=("some-model", fake_provider)):
        await model_router.generate("p", role="model_general", think=False)
    assert captured.get("think") is False


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
# Sprint I.1 — OllamaProvider.stream_chat (line-delimited JSON)
# ---------------------------------------------------------------------------
class _FakeOllamaStreamResp:
    def __init__(self, status_code=200, lines=None, body=b""):
        self.status_code = status_code
        self._lines = lines or []
        self._body = body

    async def aread(self):
        return self._body

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeOllamaStreamCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


def _fake_ollama_stream(status_code=200, lines=None, body=b""):
    def _factory(*args, **kwargs):
        return _FakeOllamaStreamCtx(
            _FakeOllamaStreamResp(status_code, lines, body),
        )
    return _factory


@pytest.mark.asyncio
async def test_ollama_stream_chat_yields_message_content():
    """Line-delimited JSON: each frame is a complete JSON object whose
    ``message.content`` is the next text delta."""
    from app import model_router
    from unittest.mock import MagicMock as _MM
    fake_client = _MM()
    fake_client.stream = _fake_ollama_stream(lines=[
        '{"model":"qwen3:4b","message":{"role":"assistant","content":"Hi "}}',
        '{"model":"qwen3:4b","message":{"role":"assistant","content":"there"}}',
        '{"model":"qwen3:4b","message":{"role":"assistant","content":""},'
        '"done":true,"total_duration":12345}',
    ])
    with patch.object(model_router, "_get_client", return_value=fake_client):
        p = OllamaProvider()
        chunks = [c async for c in p.stream_chat(
            "qwen3:4b", [{"role": "user", "content": "hi"}],
        )]
    assert chunks == ["Hi ", "there"]


@pytest.mark.asyncio
async def test_ollama_stream_chat_stops_when_done_true():
    """``done: true`` is the terminator — anything after it must NOT be
    yielded, even if more frames arrive on the wire."""
    from app import model_router
    from unittest.mock import MagicMock as _MM
    fake_client = _MM()
    fake_client.stream = _fake_ollama_stream(lines=[
        '{"message":{"content":"first"}}',
        '{"message":{"content":""},"done":true}',
        '{"message":{"content":"should-not-appear"}}',
    ])
    with patch.object(model_router, "_get_client", return_value=fake_client):
        p = OllamaProvider()
        chunks = [c async for c in p.stream_chat(
            "qwen3:4b", [{"role": "user", "content": "x"}],
        )]
    assert chunks == ["first"]


@pytest.mark.asyncio
async def test_ollama_stream_chat_skips_malformed_and_blank_lines():
    """A single malformed JSON frame must not crash the stream — Ollama
    sometimes interleaves heartbeat output that isn't valid JSON. Blank
    lines are also no-ops."""
    from app import model_router
    from unittest.mock import MagicMock as _MM
    fake_client = _MM()
    fake_client.stream = _fake_ollama_stream(lines=[
        '{"message":{"content":"ok-1"}}',
        "",
        "NOT JSON",
        '{"message":{"content":"ok-2"}}',
        '{"message":{"content":""},"done":true}',
    ])
    with patch.object(model_router, "_get_client", return_value=fake_client):
        p = OllamaProvider()
        chunks = [c async for c in p.stream_chat(
            "qwen3:4b", [{"role": "user", "content": "x"}],
        )]
    assert chunks == ["ok-1", "ok-2"]


@pytest.mark.asyncio
async def test_ollama_stream_chat_raises_on_non_200():
    """Upstream errors must surface as ProviderUnavailableError so the
    enrichment in model_router._format_provider_error can catch them on
    the role= path. Status + body snippet are included in the message."""
    from app import model_router
    from unittest.mock import MagicMock as _MM
    fake_client = _MM()
    fake_client.stream = _fake_ollama_stream(
        status_code=500,
        body=b'{"error":"out of memory"}',
    )
    with patch.object(model_router, "_get_client", return_value=fake_client):
        p = OllamaProvider()
        with pytest.raises(ProviderUnavailableError, match="ollama HTTP 500"):
            agen = p.stream_chat("qwen3:4b", [{"role": "user", "content": "x"}])
            await agen.__anext__()


@pytest.mark.asyncio
async def test_ollama_stream_chat_payload_sets_stream_true():
    """The payload must include stream=True or Ollama returns a single
    non-streaming JSON instead of the line-delimited shape we parse."""
    from app import model_router
    from unittest.mock import MagicMock as _MM
    captured: dict = {}

    def _capture(*args, **kwargs):
        captured.update(kwargs)
        return _FakeOllamaStreamCtx(_FakeOllamaStreamResp(
            200, lines=['{"message":{"content":""},"done":true}']
        ))

    fake_client = _MM()
    fake_client.stream = _capture
    with patch.object(model_router, "_get_client", return_value=fake_client):
        p = OllamaProvider()
        async for _ in p.stream_chat(
            "qwen3:4b", [{"role": "user", "content": "x"}],
        ):
            pass
    assert captured["json"]["stream"] is True
    assert captured["json"]["model"] == "qwen3:4b"


# ---------------------------------------------------------------------------
# Sprint I.2 — OllamaProvider.tool_call (POST /api/chat with tools=[...])
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


def _ollama_resp(payload: dict) -> MagicMock:
    r = MagicMock()
    r.status_code = 200
    r.text = ""
    r.json = MagicMock(return_value=payload)
    return r


@pytest.mark.asyncio
async def test_ollama_tool_call_translates_tools_to_wire_shape():
    """Tools must reach Ollama in the OpenAI-compatible structure
    ({type:function, function:{name, description, parameters}}) — that's
    the format Ollama 0.3+ expects."""
    from app import model_router
    from unittest.mock import MagicMock as _MM
    fake_client = _MM()
    fake_client.post = AsyncMock(return_value=_ollama_resp({
        "message": {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "search_web", "arguments": {"query": "rag"}}},
        ]},
    }))
    with patch.object(model_router, "_get_client", return_value=fake_client):
        p = OllamaProvider()
        await p.tool_call("qwen2.5:7b", [{"role": "user", "content": "search"}],
                          [_TOOL_SEARCH])

    args, kwargs = fake_client.post.call_args
    payload = kwargs["json"]
    assert payload["tools"] == [{
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Search the web for recent results",
            "parameters": _TOOL_SEARCH.input_schema,
        },
    }]
    assert payload["stream"] is False


@pytest.mark.asyncio
async def test_ollama_tool_call_parses_dict_arguments():
    """Ollama emits ``arguments`` as a dict directly (NOT a JSON-encoded
    string like OpenAI). The parser must handle that shape natively."""
    from app import model_router
    from unittest.mock import MagicMock as _MM
    fake_client = _MM()
    fake_client.post = AsyncMock(return_value=_ollama_resp({
        "message": {"content": "", "tool_calls": [
            {"function": {"name": "search_web",
                          "arguments": {"query": "test"}}},
            {"function": {"name": "search_web",
                          "arguments": {"query": "second"}}},
        ]},
    }))
    with patch.object(model_router, "_get_client", return_value=fake_client):
        p = OllamaProvider()
        resp = await p.tool_call("qwen2.5:7b", [{"role": "user", "content": "x"}],
                                 [_TOOL_SEARCH])

    assert resp.success is True
    assert len(resp.tool_calls) == 2
    assert resp.tool_calls[0].name == "search_web"
    assert resp.tool_calls[0].arguments == {"query": "test"}
    # Ollama doesn't emit ids — provider synthesizes tool_<index>.
    assert resp.tool_calls[0].id == "tool_0"
    assert resp.tool_calls[1].id == "tool_1"


@pytest.mark.asyncio
async def test_ollama_tool_call_handles_string_encoded_arguments():
    """Compatibility shims (or older Ollama builds) sometimes emit
    arguments as a JSON string — the parser must decode those too."""
    from app import model_router
    from unittest.mock import MagicMock as _MM
    fake_client = _MM()
    fake_client.post = AsyncMock(return_value=_ollama_resp({
        "message": {"content": "", "tool_calls": [
            {"function": {"name": "search_web",
                          "arguments": '{"query": "encoded"}'}},
        ]},
    }))
    with patch.object(model_router, "_get_client", return_value=fake_client):
        p = OllamaProvider()
        resp = await p.tool_call("qwen2.5:7b", [{"role": "user", "content": "x"}],
                                 [_TOOL_SEARCH])
    assert resp.tool_calls[0].arguments == {"query": "encoded"}


@pytest.mark.asyncio
async def test_ollama_tool_call_text_only_response_has_empty_tool_calls():
    """Models that don't support tools (or choose not to call any) emit a
    plain text response. tool_calls must be an empty list, not None."""
    from app import model_router
    from unittest.mock import MagicMock as _MM
    fake_client = _MM()
    fake_client.post = AsyncMock(return_value=_ollama_resp({
        "message": {"role": "assistant", "content": "I don't know."},
    }))
    with patch.object(model_router, "_get_client", return_value=fake_client):
        p = OllamaProvider()
        resp = await p.tool_call("qwen3:4b", [{"role": "user", "content": "x"}],
                                 [_TOOL_SEARCH])
    assert resp.success is True
    assert resp.text == "I don't know."
    assert resp.tool_calls == []


@pytest.mark.asyncio
async def test_ollama_tool_call_returns_failure_on_non_200():
    from app import model_router
    from unittest.mock import MagicMock as _MM
    fake_client = _MM()
    bad = MagicMock()
    bad.status_code = 500
    bad.text = "out of memory"
    fake_client.post = AsyncMock(return_value=bad)
    with patch.object(model_router, "_get_client", return_value=fake_client):
        p = OllamaProvider()
        resp = await p.tool_call("qwen2.5:7b", [{"role": "user", "content": "x"}],
                                 [_TOOL_SEARCH])
    assert resp.success is False
    assert "HTTP 500" in resp.error
    assert resp.tool_calls == []


# ---------------------------------------------------------------------------
# Sprint I.2 — base class default + capability gate
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_base_tool_call_default_raises_capability_error():
    """A provider that doesn't override tool_call must surface a clear
    error rather than silently returning empty tool_calls."""
    class _Embedder(LLMProvider):
        name = "embedonly_concrete"
        supports_chat = False
        supports_embeddings = True
        supports_native_tools = False

        async def chat_completion(self, model, messages, **opts):
            raise NotImplementedError

        async def list_models(self):
            return []

    p = _Embedder()
    with pytest.raises(ProviderCapabilityError, match="tool calls"):
        await p.tool_call("m", [{"role": "user", "content": "x"}], [_TOOL_SEARCH])


def test_ollama_capability_flag_now_advertises_tools():
    """OllamaProvider.supports_native_tools flipped to True in Sprint I.2.
    Locking that in a test prevents accidental regressions."""
    p = OllamaProvider()
    assert p.supports_native_tools is True


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


# ---------------------------------------------------------------------------
# §17.349 — Pydantic Literal validation on MODEL_<ROLE>_PROVIDER
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_provider_name_literal_rejects_typo(monkeypatch):
    """A typo in MODEL_<ROLE>_PROVIDER must fail at orchestrator boot
    (ValidationError) instead of silently surviving until first dispatch
    raises ProviderError. Mirrors the .env line `MODEL_GENERAL_PROVIDER=
    anthrpoic` (note the typo) — pre-§17.349 the field was `str` so it
    parsed as the literal string and only failed at provider_for_role()
    lookup. Now typed Literal["ollama","openai","anthropic"]."""
    import pydantic
    from app.config import Settings
    monkeypatch.setenv("MODEL_GENERAL_PROVIDER", "anthrpoic")  # deliberate typo
    monkeypatch.setenv("POSTGRES_PASSWORD", "test")             # required by Settings
    with pytest.raises(pydantic.ValidationError) as exc_info:
        Settings()
    err = str(exc_info.value)
    # Pydantic surfaces the allowed values in the error message
    assert "ollama" in err
    assert "openai" in err
    assert "anthropic" in err


@pytest.mark.smoke
def test_provider_name_literal_accepts_all_three_providers(monkeypatch):
    """The three registered providers must all parse cleanly."""
    from app.config import Settings
    monkeypatch.setenv("POSTGRES_PASSWORD", "test")
    for prov in ("ollama", "openai", "anthropic"):
        monkeypatch.setenv("MODEL_GENERAL_PROVIDER", prov)
        s = Settings()
        assert s.model_general_provider == prov


@pytest.mark.asyncio
async def test_ollama_provider_chat_no_think_key_by_default():
    """§17.876 — chat_completion leaves the model's own thinking default when
    think is not passed (parity with generate's §17.683 contract)."""
    p = OllamaProvider()
    fake = AsyncMock(return_value=ModelResponse(
        text="hi", model="m", success=True, provider="ollama",
    ))
    from app import model_router
    with patch.object(model_router, "_call_ollama", side_effect=fake):
        await p.chat_completion("m", [{"role": "user", "content": "u"}])
    args, _ = fake.call_args
    assert args[0] == "/api/chat"
    assert "think" not in args[1]


@pytest.mark.asyncio
async def test_ollama_provider_chat_think_false_threads_into_payload():
    """§17.876 — think=False must reach the /api/chat payload. Live incident:
    the assist fix prompt drove chain-of-thought past the whole 8192 budget on
    all draws → "(no fix returned)"; chat had no way to disable thinking."""
    p = OllamaProvider()
    fake = AsyncMock(return_value=ModelResponse(
        text="hi", model="m", success=True, provider="ollama",
    ))
    from app import model_router
    with patch.object(model_router, "_call_ollama", side_effect=fake):
        await p.chat_completion("m", [{"role": "user", "content": "u"}], think=False)
    payload = fake.call_args[0][1]
    assert payload["think"] is False


@pytest.mark.asyncio
async def test_model_router_chat_think_threads_through_role_path():
    """§17.876 — model_router.chat(role=..., think=False) forwards think to the
    provider (the §17.876 think-off rescue in llm_retry relies on this)."""
    from app import model_router
    captured = {}

    async def _fake_chat(model, messages, **kwargs):
        captured.update(kwargs)
        return ModelResponse(text="hi", model=model, success=True, provider="ollama")

    fake_provider = MagicMock()
    fake_provider.chat_completion = AsyncMock(side_effect=_fake_chat)
    with patch.object(model_router, "_resolve_role",
                      return_value=("some-model", fake_provider)):
        await model_router.chat(
            [{"role": "user", "content": "u"}], role="model_general", think=False,
        )
    assert captured.get("think") is False


@pytest.mark.asyncio
async def test_model_router_chat_legacy_path_threads_think():
    """§17.876 — the legacy direct-Ollama chat path threads think too."""
    from app import model_router
    fake = AsyncMock(return_value=ModelResponse(
        text="hi", model="m", success=True, provider="ollama",
    ))
    with patch.object(model_router, "_dispatch_with_retry", side_effect=fake):
        await model_router.chat(
            [{"role": "user", "content": "u"}], model="m", think=False,
        )
    payload = fake.call_args[0][1]
    assert payload["think"] is False
