"""Tests for app/model_router.py (#9.21)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app import model_router


# ---------------------------------------------------------------------------
# Cloud detection + timeout routing
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_is_cloud_recognizes_cloud_suffix():
    assert model_router._is_cloud("some-model-cloud") is True


@pytest.mark.smoke
def test_is_cloud_recognizes_configured_cloud_models():
    from app.config import settings
    assert model_router._is_cloud(settings.model_cloud_alt) is True
    assert model_router._is_cloud(settings.model_cloud_heavy) is True


@pytest.mark.smoke
def test_is_cloud_false_for_local_model():
    assert model_router._is_cloud("qwen2.5:7b") is False


@pytest.mark.smoke
def test_timeout_for_cloud_uses_cloud_timeout():
    from app.config import settings
    assert model_router._timeout_for("anything-cloud") == settings.cloud_timeout


@pytest.mark.smoke
def test_timeout_for_local_uses_local_timeout():
    from app.config import settings
    assert model_router._timeout_for("qwen2.5:7b") == settings.local_timeout


# ---------------------------------------------------------------------------
# Smart fallback
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_smart_fallback_routes_code_models_to_coder():
    from app.config import settings
    assert model_router._smart_fallback("codeCustom", "x") == settings.model_coder
    assert model_router._smart_fallback("custom-coder", "x") == settings.model_coder
    assert model_router._smart_fallback("codegen:7b", "x") == settings.model_coder


@pytest.mark.smoke
def test_smart_fallback_returns_default_for_non_code():
    assert model_router._smart_fallback("qwen3:4b", "default-xyz") == "default-xyz"


# ---------------------------------------------------------------------------
# _call_ollama — response parsing + error paths
# ---------------------------------------------------------------------------
def _mk_response(status: int, payload: dict | None = None, text: str = ""):
    """Build a fake httpx.Response-like object."""
    r = MagicMock()
    r.status_code = status
    r.text = text
    r.json = MagicMock(return_value=payload or {})
    return r


@pytest.mark.smoke
async def test_call_ollama_returns_success_on_200():
    fake_client = AsyncMock()
    fake_client.post.return_value = _mk_response(200, {
        "response": "hi there",
        "prompt_eval_count": 5,
        "eval_count": 10,
        "eval_duration": 1_000_000_000,  # 1 second in ns
    })
    with patch.object(model_router, "_get_client", return_value=fake_client):
        resp = await model_router._call_ollama("/api/generate", {}, "m", 30)
    assert resp.success is True
    assert resp.text == "hi there"
    assert resp.tokens_completion == 10
    assert resp.tokens_per_sec == 10.0  # 10 tokens / 1 second


@pytest.mark.smoke
async def test_call_ollama_returns_failure_on_non_200():
    fake_client = AsyncMock()
    fake_client.post.return_value = _mk_response(500, {}, text="upstream down")
    with patch.object(model_router, "_get_client", return_value=fake_client):
        resp = await model_router._call_ollama("/api/generate", {}, "m", 30)
    assert resp.success is False
    assert "HTTP 500" in resp.error


@pytest.mark.smoke
async def test_call_ollama_handles_timeout():
    fake_client = AsyncMock()
    fake_client.post.side_effect = httpx.TimeoutException("boom")
    with patch.object(model_router, "_get_client", return_value=fake_client):
        resp = await model_router._call_ollama("/api/generate", {}, "m", 5)
    assert resp.success is False
    assert "Timeout" in resp.error


@pytest.mark.smoke
async def test_call_ollama_handles_chat_format():
    """/api/chat returns message.content instead of response."""
    fake_client = AsyncMock()
    fake_client.post.return_value = _mk_response(200, {
        "message": {"content": "chat reply"},
    })
    with patch.object(model_router, "_get_client", return_value=fake_client):
        resp = await model_router._call_ollama("/api/chat", {}, "m", 30)
    assert resp.text == "chat reply"


# ---------------------------------------------------------------------------
# Retry cascade
# ---------------------------------------------------------------------------
@pytest.mark.smoke
async def test_dispatch_retries_on_failure_then_falls_back():
    """Primary model fails max_retries times, fallback succeeds."""
    call_log = []

    async def fake_call(endpoint, payload, model, timeout):
        call_log.append(model)
        if model == "primary":
            return model_router.ModelResponse(model=model, success=False, error="fail")
        return model_router.ModelResponse(model=model, text="fallback ok", success=True)

    with patch.object(model_router, "_call_ollama", side_effect=fake_call):
        resp = await model_router._dispatch_with_retry(
            "/api/generate", {}, "primary", fallback="fallback", max_retries=3,
        )
    assert resp.success is True
    assert resp.fallback_used is True
    assert resp.text == "fallback ok"
    assert call_log == ["primary", "primary", "primary", "fallback"]


@pytest.mark.smoke
async def test_dispatch_stops_early_on_success():
    """Primary succeeds on attempt 1 — no further calls."""
    call_log = []

    async def fake_call(endpoint, payload, model, timeout):
        call_log.append(model)
        return model_router.ModelResponse(model=model, text="ok", success=True)

    with patch.object(model_router, "_call_ollama", side_effect=fake_call):
        resp = await model_router._dispatch_with_retry(
            "/api/generate", {}, "primary", fallback="fallback", max_retries=3,
        )
    assert resp.retries == 0
    assert resp.fallback_used is False
    assert call_log == ["primary"]


@pytest.mark.smoke
async def test_dispatch_skips_fallback_when_same_as_primary():
    """If fallback == primary, no second phase."""
    call_log = []

    async def fake_call(endpoint, payload, model, timeout):
        call_log.append(model)
        return model_router.ModelResponse(model=model, success=False, error="nope")

    with patch.object(model_router, "_call_ollama", side_effect=fake_call):
        resp = await model_router._dispatch_with_retry(
            "/api/generate", {}, "same", fallback="same", max_retries=2,
        )
    assert resp.success is False
    assert call_log == ["same", "same"]  # only 2 attempts, no 3rd for fallback


# ---------------------------------------------------------------------------
# Public API shape
# ---------------------------------------------------------------------------
@pytest.mark.smoke
async def test_embed_returns_empty_on_failure():
    with patch.object(
        model_router, "_dispatch_with_retry",
        AsyncMock(return_value=model_router.ModelResponse(
            model="m", success=False, error="down",
        )),
    ):
        result = await model_router.embed("test")
    assert result == []


@pytest.mark.smoke
async def test_embed_returns_vectors_on_success():
    with patch.object(
        model_router, "_dispatch_with_retry",
        AsyncMock(return_value=model_router.ModelResponse(
            model="m", success=True, raw={"embeddings": [[0.1, 0.2], [0.3, 0.4]]},
        )),
    ):
        result = await model_router.embed(["a", "b"])
    assert result == [[0.1, 0.2], [0.3, 0.4]]


# ---------------------------------------------------------------------------
# list_models / validate_models
# ---------------------------------------------------------------------------
@pytest.mark.smoke
async def test_list_models_returns_empty_on_error():
    fake_client = AsyncMock()
    fake_client.get.side_effect = RuntimeError("connection refused")
    with patch.object(model_router, "_get_client", return_value=fake_client):
        result = await model_router.list_models()
    assert result == []


@pytest.mark.smoke
async def test_list_models_returns_names_on_success():
    fake_client = AsyncMock()
    resp = MagicMock()
    resp.json.return_value = {"models": [{"name": "a:latest"}, {"name": "b:7b"}]}
    fake_client.get.return_value = resp
    with patch.object(model_router, "_get_client", return_value=fake_client):
        result = await model_router.list_models()
    assert result == ["a:latest", "b:7b"]


@pytest.mark.smoke
async def test_validate_models_returns_missing_list():
    fake_client = AsyncMock()
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"models": [{"name": "qwen3:4b"}]}
    fake_client.get.return_value = resp
    with patch.object(model_router, "_get_client", return_value=fake_client):
        missing = await model_router.validate_models()
    # Most role tags won't be in our 1-model fake response; expect a list
    assert isinstance(missing, list)
    assert len(missing) >= 1  # at least one role isn't qwen3:4b


# ---------------------------------------------------------------------------
# Sprint E — ModelResponse stamps provider="ollama" on every path; the
# canonical class is now app.providers.base.ModelResponse and the
# model_router re-export must remain identity-equal to it.
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_model_response_reexport_is_canonical():
    from app.providers.base import ModelResponse as Canonical
    assert model_router.ModelResponse is Canonical


@pytest.mark.smoke
async def test_call_ollama_stamps_provider_on_success():
    fake_client = AsyncMock()
    fake_client.post.return_value = _mk_response(200, {"response": "ok"})
    with patch.object(model_router, "_get_client", return_value=fake_client):
        resp = await model_router._call_ollama("/api/generate", {}, "m", 30)
    assert resp.provider == "ollama"


@pytest.mark.smoke
async def test_call_ollama_stamps_provider_on_http_error():
    fake_client = AsyncMock()
    fake_client.post.return_value = _mk_response(500, {}, text="boom")
    with patch.object(model_router, "_get_client", return_value=fake_client):
        resp = await model_router._call_ollama("/api/generate", {}, "m", 30)
    assert resp.provider == "ollama"


@pytest.mark.smoke
async def test_call_ollama_stamps_provider_on_timeout():
    fake_client = AsyncMock()
    fake_client.post.side_effect = httpx.TimeoutException("boom")
    with patch.object(model_router, "_get_client", return_value=fake_client):
        resp = await model_router._call_ollama("/api/generate", {}, "m", 5)
    assert resp.provider == "ollama"


@pytest.mark.smoke
async def test_call_ollama_stamps_provider_on_unexpected_exception():
    fake_client = AsyncMock()
    fake_client.post.side_effect = RuntimeError("kaboom")
    with patch.object(model_router, "_get_client", return_value=fake_client):
        resp = await model_router._call_ollama("/api/generate", {}, "m", 5)
    assert resp.provider == "ollama"


@pytest.mark.smoke
async def test_dispatch_with_retry_initial_response_carries_provider():
    """If every attempt errors before _call_ollama returns, the synthetic
    'no attempt' starter must still carry provider='ollama' so downstream
    metric capture has a non-empty field."""
    async def always_fail(endpoint, payload, model, timeout):
        return model_router.ModelResponse(
            model=model, success=False, error="x", provider="ollama",
        )
    with patch.object(model_router, "_call_ollama", side_effect=always_fail):
        resp = await model_router._dispatch_with_retry(
            "/api/generate", {}, "same", fallback="same", max_retries=1,
        )
    assert resp.provider == "ollama"


# ---------------------------------------------------------------------------
# Sprint E.7 — role= dispatches through the provider abstraction.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_generate_role_routes_through_provider():
    """role= resolves to (model, provider) and dispatches through the
    provider's generate(); patching _call_ollama still intercepts because
    OllamaProvider delegates back into model_router."""
    fake = AsyncMock(return_value=model_router.ModelResponse(
        text="role-routed", model="m", success=True, provider="ollama",
    ))
    with patch.object(model_router, "_call_ollama", side_effect=fake):
        resp = await model_router.generate("hi", role="model_general")
    assert resp.success is True
    assert resp.text == "role-routed"
    assert resp.provider == "ollama"


@pytest.mark.asyncio
async def test_chat_role_routes_through_provider():
    fake = AsyncMock(return_value=model_router.ModelResponse(
        text="ok", model="m", success=True, provider="ollama",
    ))
    with patch.object(model_router, "_call_ollama", side_effect=fake):
        resp = await model_router.chat(
            [{"role": "user", "content": "hi"}], role="model_verifier",
        )
    assert resp.success is True
    args, _ = fake.call_args
    assert args[0] == "/api/chat"


@pytest.mark.asyncio
async def test_embed_role_returns_list_of_vectors():
    embedding = [[0.1, 0.2, 0.3]]
    fake = AsyncMock(return_value=model_router.ModelResponse(
        model="m", success=True, provider="ollama",
        raw={"embeddings": embedding},
    ))
    with patch.object(model_router, "_call_ollama", side_effect=fake):
        result = await model_router.embed("hello", role="model_embedder_pipeline")
    assert result == embedding
    args, _ = fake.call_args
    assert args[0] == "/api/embed"


@pytest.mark.asyncio
async def test_classify_default_routes_through_router_role():
    """classify() with no args must route through model_router role —
    preserves prior default of using settings.model_router but now via
    the provider seam."""
    from app.config import settings
    captured: dict = {}

    async def fake(endpoint, payload, model, timeout):
        captured["model"] = model
        captured["endpoint"] = endpoint
        return model_router.ModelResponse(
            text="cls", model=model, success=True, provider="ollama",
        )
    with patch.object(model_router, "_call_ollama", side_effect=fake):
        resp = await model_router.classify("classify this")
    assert resp.success is True
    assert captured["model"] == settings.model_router
    assert captured["endpoint"] == "/api/generate"


@pytest.mark.asyncio
async def test_role_and_model_together_raises():
    with pytest.raises(ValueError, match="not both"):
        await model_router.generate("p", model="x", role="model_general")
    with pytest.raises(ValueError, match="not both"):
        await model_router.chat(
            [{"role": "user", "content": "x"}], model="x", role="model_general",
        )
    with pytest.raises(ValueError, match="not both"):
        await model_router.embed("x", model="m", role="model_embedder_pipeline")


@pytest.mark.asyncio
async def test_role_with_overrides_resolves_model_and_provider():
    """overrides dict feeds both get_model() and provider_for_role()."""
    fake = AsyncMock(return_value=model_router.ModelResponse(
        text="ok", model="custom-tag", success=True, provider="ollama",
    ))
    with patch.object(model_router, "_call_ollama", side_effect=fake):
        await model_router.generate(
            "hi",
            role="model_general",
            overrides={"model_general": "custom-tag",
                       "model_general_provider": "ollama"},
        )
    args, _ = fake.call_args
    assert args[2] == "custom-tag"


@pytest.mark.asyncio
async def test_legacy_model_arg_still_works():
    """Existing callers passing model=... must keep working unchanged."""
    fake = AsyncMock(return_value=model_router.ModelResponse(
        text="legacy", model="qwen", success=True, provider="ollama",
    ))
    with patch.object(model_router, "_call_ollama", side_effect=fake):
        resp = await model_router.generate("p", model="qwen")
    assert resp.text == "legacy"
    args, _ = fake.call_args
    assert args[2] == "qwen"
