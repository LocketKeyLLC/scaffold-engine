"""Tests for the native OpenAI-compatible surface — /v1 (§17.788, Phase 0).

Endpoint tests drive the ``openai_app`` sub-app directly with a TestClient
(auth bypassed via dependency_overrides; ``model_router`` mocked so no network),
plus pure-unit coverage of the wire-type builders in ``app.openai_schemas``.
"""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import openai_schemas as oai
from app.auth import require_openai_key
from app.providers.base import ModelResponse
from app.routers.openai_compat import openai_app


# ── Wire-type builders (pure unit) ────────────────────────────────────────────
@pytest.mark.smoke
def test_message_text_passthrough_string():
    assert oai.message_text("hello") == "hello"


@pytest.mark.smoke
def test_message_text_flattens_content_parts():
    content = [
        {"type": "text", "text": "a"},
        {"type": "image_url", "image_url": {"url": "x"}},  # dropped
        {"type": "text", "text": "b"},
    ]
    assert oai.message_text(content) == "ab"


@pytest.mark.smoke
def test_message_text_none_is_empty():
    assert oai.message_text(None) == ""


@pytest.mark.smoke
def test_to_router_messages_shape():
    req = oai.ChatCompletionRequest(
        model="scaffold-engine",
        messages=[
            oai.ChatMessage(role="system", content="sys"),
            oai.ChatMessage(role="user", content=[{"type": "text", "text": "hi"}]),
        ],
    )
    out = oai.to_router_messages(req.messages)
    assert out == [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]


@pytest.mark.smoke
def test_request_tolerates_unknown_openai_fields():
    """A stock OpenAI client sends n/presence_penalty/user/seed — must not 422."""
    req = oai.ChatCompletionRequest(
        model="scaffold-engine",
        messages=[oai.ChatMessage(role="user", content="hi")],
        n=1,
        presence_penalty=0.0,
        user="abc",
        seed=7,
    )
    assert req.model == "scaffold-engine"


@pytest.mark.smoke
def test_resolved_max_tokens_prefers_completion_tokens():
    req = oai.ChatCompletionRequest(
        model="m", messages=[], max_tokens=100, max_completion_tokens=200
    )
    assert req.resolved_max_tokens(4096) == 200
    req2 = oai.ChatCompletionRequest(model="m", messages=[], max_tokens=100)
    assert req2.resolved_max_tokens(4096) == 100
    req3 = oai.ChatCompletionRequest(model="m", messages=[])
    assert req3.resolved_max_tokens(4096) == 4096


@pytest.mark.smoke
def test_completion_response_shape():
    obj = oai.completion_response(
        completion_id="chatcmpl-x", model="scaffold-engine", content="hello",
        prompt_tokens=5, completion_tokens=2,
    )
    assert obj["object"] == "chat.completion"
    assert obj["choices"][0]["message"] == {"role": "assistant", "content": "hello"}
    assert obj["choices"][0]["finish_reason"] == "stop"
    assert obj["usage"] == {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}


@pytest.mark.smoke
def test_chunk_shape():
    c = oai.chunk(completion_id="chatcmpl-x", model="m", delta={"content": "hi"})
    assert c["object"] == "chat.completion.chunk"
    assert c["choices"][0]["delta"] == {"content": "hi"}
    assert c["choices"][0]["finish_reason"] is None


# ── Endpoints ─────────────────────────────────────────────────────────────────
@pytest.fixture
def client():
    """TestClient over the /v1 sub-app with auth bypassed."""
    openai_app.dependency_overrides[require_openai_key] = lambda: "testkey"
    try:
        yield TestClient(openai_app)
    finally:
        openai_app.dependency_overrides.clear()


@pytest.mark.smoke
def test_list_models_advertises_scaffold_engine(client):
    r = client.get("/models")
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    ids = [m["id"] for m in body["data"]]
    assert "scaffold-engine" in ids


@pytest.mark.smoke
def test_chat_completion_non_stream(client):
    fake = ModelResponse(text="Hello world", success=True,
                         tokens_prompt=5, tokens_completion=2)
    with patch("app.native_chat.route", AsyncMock(return_value=None)), \
         patch("app.model_router.chat", AsyncMock(return_value=fake)):
        r = client.post("/chat/completions", json={
            "model": "scaffold-engine",
            "messages": [{"role": "user", "content": "hi"}],
        })
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "Hello world"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["total_tokens"] == 7
    assert body["model"] == "scaffold-engine"


@pytest.mark.smoke
def test_chat_completion_generation_failure_is_openai_error(client):
    fake = ModelResponse(text="", success=False, error="backend down")
    with patch("app.native_chat.route", AsyncMock(return_value=None)), \
         patch("app.model_router.chat", AsyncMock(return_value=fake)):
        r = client.post("/chat/completions", json={
            "model": "scaffold-engine",
            "messages": [{"role": "user", "content": "hi"}],
        })
    assert r.status_code == 502
    body = r.json()
    assert "error" in body
    assert body["error"]["type"] == "api_error"


@pytest.mark.smoke
def test_chat_completion_stream(client):
    async def _fake_stream(*_a, **_k):
        for piece in ["Hel", "lo"]:
            yield piece

    with patch("app.native_chat.route", AsyncMock(return_value=None)), \
         patch("app.model_router.stream_chat", _fake_stream):
        r = client.post("/chat/completions", json={
            "model": "scaffold-engine",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        })
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    body = r.text
    # role delta first, content deltas, terminal [DONE]
    assert '"role": "assistant"' in body or '"role":"assistant"' in body
    assert "Hel" in body and "lo" in body
    assert "data: [DONE]" in body
    assert '"finish_reason": "stop"' in body or '"finish_reason":"stop"' in body


@pytest.mark.smoke
def test_401_renders_openai_error_envelope():
    """A failed auth on /v1 must return the OpenAI ``{"error": {...}}`` envelope
    (not FastAPI's ``{"detail": ...}``) so stock OpenAI SDKs parse it."""
    def _reject():
        raise StarletteHTTPException(status_code=401, detail="Invalid or missing API key")

    openai_app.dependency_overrides[require_openai_key] = _reject
    try:
        r = TestClient(openai_app).get("/models")
    finally:
        openai_app.dependency_overrides.clear()
    assert r.status_code == 401
    body = r.json()
    assert "error" in body and "detail" not in body
    assert body["error"]["type"] == "authentication_error"
    assert body["error"]["message"] == "Invalid or missing API key"
