"""§17.773 — grammar-constrained decoding (structured outputs).

Covers the Phase-1 plumbing: the provider-agnostic ``response_schema`` is
translated to each backend's native constraint (Ollama ``format``, OpenAI
``response_format`` json_schema, Anthropic ``output_config.format``), the
``structured_outputs_enabled`` valve gates the whole feature at a single
chokepoint in ``model_router``, and ``generate_json`` returns
``(parsed, resp)`` with the json_repair fallback intact.

No live HTTP — the shared dispatch/client seams are mocked.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import model_router
from app.config import settings
from app.providers.anthropic import AnthropicProvider
from app.providers.base import ModelResponse
from app.providers.ollama import OllamaProvider, _apply_ollama_format
from app.providers.openai import OpenAIProvider, _apply_openai_response_format

_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}},
    "required": ["name"],
    "additionalProperties": False,
}


@pytest.fixture
def structured_on():
    """Enable the default-off valve for the duration of a test."""
    saved = settings.structured_outputs_enabled
    settings.structured_outputs_enabled = True
    try:
        yield
    finally:
        settings.structured_outputs_enabled = saved


def test_provider_structured_output_capability_flags():
    """OpenAI/Anthropic enforce schemas; Ollama does not (cloud proxy)."""
    assert OpenAIProvider().supports_structured_outputs is True
    assert AnthropicProvider().supports_structured_outputs is True
    assert OllamaProvider().supports_structured_outputs is False


# ---------------------------------------------------------------------------
# Ollama — format field
# ---------------------------------------------------------------------------
def test_apply_ollama_format_dict_and_string_and_none():
    p = {}
    _apply_ollama_format(p, _SCHEMA)
    assert p["format"] == _SCHEMA
    p = {}
    _apply_ollama_format(p, "json")
    assert p["format"] == "json"
    p = {}
    _apply_ollama_format(p, None)
    assert "format" not in p


@pytest.mark.asyncio
async def test_ollama_chat_completion_threads_format():
    captured = {}

    async def _fake(endpoint, payload, model, fallback):
        captured["payload"] = payload
        return ModelResponse(model=model, success=True, text="{}")

    with patch.object(model_router, "_dispatch_with_retry", side_effect=_fake):
        await OllamaProvider().chat_completion(
            "kimi", [{"role": "user", "content": "hi"}], response_schema=_SCHEMA,
        )
    assert captured["payload"]["format"] == _SCHEMA


@pytest.mark.asyncio
async def test_ollama_generate_threads_format_and_none_is_noop():
    captured = {}

    async def _fake(endpoint, payload, model, fallback):
        captured["payload"] = payload
        return ModelResponse(model=model, success=True, text="{}")

    with patch.object(model_router, "_dispatch_with_retry", side_effect=_fake):
        await OllamaProvider().generate("kimi", "hi", response_schema=_SCHEMA)
    assert captured["payload"]["format"] == _SCHEMA

    with patch.object(model_router, "_dispatch_with_retry", side_effect=_fake):
        await OllamaProvider().generate("kimi", "hi")  # no schema
    assert "format" not in captured["payload"]


# ---------------------------------------------------------------------------
# OpenAI — response_format
# ---------------------------------------------------------------------------
def test_apply_openai_response_format_dict_string_none():
    p = {}
    _apply_openai_response_format(p, _SCHEMA)
    assert p["response_format"]["type"] == "json_schema"
    assert p["response_format"]["json_schema"]["schema"] == _SCHEMA
    # §17.789 — _SCHEMA qualifies for strict mode (object, additionalProperties
    # false, required == all property keys), so strict is now enabled.
    assert p["response_format"]["json_schema"]["strict"] is True
    p = {}
    _apply_openai_response_format(p, "json")
    assert p["response_format"] == {"type": "json_object"}
    p = {}
    _apply_openai_response_format(p, None)
    assert "response_format" not in p


def test_apply_openai_response_format_non_strict_schema_stays_false():
    """§17.789 — a schema that doesn't satisfy strict-mode requirements (missing
    additionalProperties:false / not all-required) falls back to strict:false."""
    lenient = {"type": "object", "properties": {"name": {"type": "string"}}}
    p = {}
    _apply_openai_response_format(p, lenient)
    assert p["response_format"]["json_schema"]["strict"] is False


@pytest.mark.asyncio
async def test_openai_chat_completion_translates_schema_not_raw():
    """``response_schema`` becomes ``response_format`` and never leaks into the
    payload as a raw key OpenAI wouldn't understand."""
    client = AsyncMock()
    r = MagicMock()
    r.status_code = 200
    r.text = ""
    r.json = MagicMock(return_value={
        "model": "gpt-4o", "choices": [{"message": {"content": "{}"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    })
    client.post.return_value = r
    from pydantic import SecretStr
    saved = settings.openai_api_key
    settings.openai_api_key = SecretStr("sk-test")
    try:
        with patch.object(OpenAIProvider, "_client", staticmethod(lambda: client)):
            await OpenAIProvider().chat_completion(
                "gpt-4o", [{"role": "user", "content": "x"}], response_schema=_SCHEMA,
            )
    finally:
        settings.openai_api_key = saved
    payload = client.post.call_args.kwargs["json"]
    assert payload["response_format"]["type"] == "json_schema"
    assert "response_schema" not in payload


# ---------------------------------------------------------------------------
# Anthropic — output_config.format
# ---------------------------------------------------------------------------
def test_anthropic_build_payload_sets_output_config():
    p = AnthropicProvider()._build_payload(
        "claude-opus-4-8", [{"role": "user", "content": "x"}],
        temperature=0.5, max_tokens=100, opts={"response_schema": _SCHEMA},
    )
    assert p["output_config"] == {
        "format": {"type": "json_schema", "schema": _SCHEMA},
    }
    assert "response_schema" not in p


def test_anthropic_explicit_output_config_wins_over_schema():
    explicit = {"format": {"type": "json_schema", "schema": {"type": "object"}}}
    p = AnthropicProvider()._build_payload(
        "claude-opus-4-8", [{"role": "user", "content": "x"}],
        temperature=0.5, max_tokens=100,
        opts={"response_schema": _SCHEMA, "output_config": explicit},
    )
    assert p["output_config"] == explicit


def test_anthropic_json_string_skipped():
    p = AnthropicProvider()._build_payload(
        "claude-opus-4-8", [{"role": "user", "content": "x"}],
        temperature=0.5, max_tokens=100, opts={"response_schema": "json"},
    )
    assert "output_config" not in p


# ---------------------------------------------------------------------------
# Provider-aware valve gate — _effective_response_schema
# ---------------------------------------------------------------------------
@pytest.fixture
def ollama_optin_on(structured_on):
    """Master valve + the Ollama opt-in sub-valve both on."""
    saved = settings.structured_outputs_ollama_enabled
    settings.structured_outputs_ollama_enabled = True
    try:
        yield
    finally:
        settings.structured_outputs_ollama_enabled = saved


def test_gate_applies_only_to_enforcing_providers_when_master_on(structured_on):
    # OpenAI / Anthropic enforce schemas server-side → schema passes through.
    assert model_router._effective_response_schema(_SCHEMA, OpenAIProvider()) == _SCHEMA
    assert model_router._effective_response_schema(_SCHEMA, AnthropicProvider()) == _SCHEMA
    # Ollama (cloud proxy ignores `format`) → dropped even with the master on.
    assert model_router._effective_response_schema(_SCHEMA, OllamaProvider()) is None
    # No schema is always None.
    assert model_router._effective_response_schema(None, OpenAIProvider()) is None


def test_gate_none_for_everyone_when_master_off():
    assert not settings.structured_outputs_enabled  # default
    assert model_router._effective_response_schema(_SCHEMA, OpenAIProvider()) is None
    assert model_router._effective_response_schema(_SCHEMA, OllamaProvider()) is None


def test_gate_ollama_optin_reenables_when_both_valves_on(ollama_optin_on):
    assert model_router._effective_response_schema(_SCHEMA, OllamaProvider()) == _SCHEMA
    # Sub-valve alone (without master) never applies — covered by master-off test.


@pytest.mark.asyncio
async def test_role_path_threads_schema_for_enforcing_provider(structured_on):
    """A role bound to an enforcing provider gets the schema — the gate runs
    AFTER role resolution, so it sees the real provider capability."""
    captured = {}
    fake = MagicMock()
    fake.name = "openai"
    fake.supports_structured_outputs = True

    async def _gen(model, prompt, **kw):
        captured["schema"] = kw.get("response_schema")
        return ModelResponse(model=model, success=True, text="{}")

    fake.generate = _gen
    with patch.object(model_router, "_resolve_role", return_value=("gpt-4o", fake)), \
         patch.object(model_router, "_record_call", AsyncMock(side_effect=lambda r: r)):
        await model_router.generate("hi", role="model_general", response_schema=_SCHEMA)
    assert captured["schema"] == _SCHEMA


@pytest.mark.asyncio
async def test_role_path_drops_schema_for_ollama(structured_on):
    """Same master valve, but an Ollama-bound role drops the schema."""
    captured = {}
    fake = MagicMock()
    fake.name = "ollama"
    fake.supports_structured_outputs = False

    async def _gen(model, prompt, **kw):
        captured["schema"] = kw.get("response_schema")
        return ModelResponse(model=model, success=True, text="{}")

    fake.generate = _gen
    with patch.object(model_router, "_resolve_role", return_value=("kimi", fake)), \
         patch.object(model_router, "_record_call", AsyncMock(side_effect=lambda r: r)):
        await model_router.generate("hi", role="model_general", response_schema=_SCHEMA)
    assert captured["schema"] is None


@pytest.mark.asyncio
async def test_generate_legacy_ollama_drops_format_even_when_master_on(structured_on):
    """Legacy direct path is Ollama → dropped unless the ollama sub-valve is on."""
    captured = {}

    async def _fake(endpoint, payload, model, fallback):
        captured["payload"] = payload
        return ModelResponse(model=model, success=True, text="{}")

    with patch.object(model_router, "_dispatch_with_retry", side_effect=_fake), \
         patch.object(model_router, "_record_call", AsyncMock(side_effect=lambda r: r)):
        await model_router.generate("hi", model="kimi", response_schema=_SCHEMA)
    assert "format" not in captured["payload"]


@pytest.mark.asyncio
async def test_generate_legacy_ollama_sets_format_when_optin_on(ollama_optin_on):
    captured = {}

    async def _fake(endpoint, payload, model, fallback):
        captured["payload"] = payload
        return ModelResponse(model=model, success=True, text="{}")

    with patch.object(model_router, "_dispatch_with_retry", side_effect=_fake), \
         patch.object(model_router, "_record_call", AsyncMock(side_effect=lambda r: r)):
        await model_router.generate("hi", model="kimi", response_schema=_SCHEMA)
    assert captured["payload"]["format"] == _SCHEMA


@pytest.mark.asyncio
async def test_generate_legacy_drops_format_when_master_off():
    captured = {}

    async def _fake(endpoint, payload, model, fallback):
        captured["payload"] = payload
        return ModelResponse(model=model, success=True, text="{}")

    with patch.object(model_router, "_dispatch_with_retry", side_effect=_fake), \
         patch.object(model_router, "_record_call", AsyncMock(side_effect=lambda r: r)):
        await model_router.generate("hi", model="kimi", response_schema=_SCHEMA)
    assert "format" not in captured["payload"]


# ---------------------------------------------------------------------------
# generate_json — parse + repair fallback
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_generate_json_returns_parsed_and_resp():
    async def _fake_generate(prompt, **kw):
        assert kw.get("response_schema") == _SCHEMA
        return ModelResponse(model="kimi", success=True, text='{"name": "ok"}')

    with patch.object(model_router, "generate", side_effect=_fake_generate):
        parsed, resp = await model_router.generate_json("hi", _SCHEMA, model="kimi")
    assert parsed == {"name": "ok"}
    assert resp.success is True


@pytest.mark.asyncio
async def test_generate_json_uses_repair_fallback_on_dirty_output():
    """A model that ignored the grammar (trailing junk / fences) still parses
    via the json_repair fallback baked into parse_json_object."""
    async def _fake_generate(prompt, **kw):
        return ModelResponse(
            model="kimi", success=True,
            text='```json\n{"name": "ok",}\n``` trailing junk',
        )

    with patch.object(model_router, "generate", side_effect=_fake_generate):
        parsed, _ = await model_router.generate_json("hi", _SCHEMA, model="kimi")
    assert parsed == {"name": "ok"}


@pytest.mark.asyncio
async def test_generate_json_array_mode():
    async def _fake_generate(prompt, **kw):
        return ModelResponse(model="kimi", success=True, text='[{"name": "a"}]')

    with patch.object(model_router, "generate", side_effect=_fake_generate):
        parsed, _ = await model_router.generate_json(
            "hi", _SCHEMA, model="kimi", as_array=True,
        )
    assert parsed == [{"name": "a"}]


@pytest.mark.asyncio
async def test_generate_json_failure_returns_none_and_resp():
    async def _fake_generate(prompt, **kw):
        return ModelResponse(model="kimi", success=False, error="boom")

    with patch.object(model_router, "generate", side_effect=_fake_generate):
        parsed, resp = await model_router.generate_json("hi", _SCHEMA, model="kimi")
    assert parsed is None
    assert resp.success is False
