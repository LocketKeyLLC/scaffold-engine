"""Tests for U.8.C additions: client.research, client.config, client.models.

Mirrors the sync + async pattern in test_typed_methods + test_async_typed_methods.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from scaffold_client import AsyncClient, Client


def _resp(status: int = 200, payload: dict | list | None = None) -> MagicMock:
    r = MagicMock(spec=httpx.Response)
    r.status_code = status
    r.text = ""
    r.json = MagicMock(return_value=payload if payload is not None else {})
    return r


def _last_call(mock):
    return mock.call_args.args, mock.call_args.kwargs


# ---------------------------------------------------------------------------
# Sync client.research.*
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    with Client("http://example.com", api_key="k") as c:
        yield c


def test_research_list_default_params(client):
    with patch.object(client._http, "request", return_value=_resp(200, {"sessions": []})) as m:
        client.research.list()
    args, kwargs = _last_call(m)
    assert args == ("GET", "/research/sessions")
    assert kwargs["params"] == {"limit": 25, "offset": 0}


def test_research_list_with_status_and_q(client):
    with patch.object(client._http, "request", return_value=_resp(200, {})) as m:
        client.research.list(status="completed", q="kubernetes", limit=50, offset=10)
    _, kwargs = _last_call(m)
    assert kwargs["params"] == {
        "status": "completed",
        "q": "kubernetes",
        "limit": 50,
        "offset": 10,
    }


def test_research_list_drops_none_filters(client):
    with patch.object(client._http, "request", return_value=_resp(200, {})) as m:
        client.research.list(status=None, q=None)
    _, kwargs = _last_call(m)
    # _drop_none should leave only the always-present limit/offset
    assert "status" not in kwargs["params"]
    assert "q" not in kwargs["params"]


def test_research_find_is_list_with_q(client):
    with patch.object(client._http, "request", return_value=_resp(200, {})) as m:
        client.research.find("k8s", limit=10)
    args, kwargs = _last_call(m)
    assert args == ("GET", "/research/sessions")
    assert kwargs["params"]["q"] == "k8s"
    assert kwargs["params"]["limit"] == 10


def test_research_rename(client):
    with patch.object(client._http, "request", return_value=_resp(200, {})) as m:
        client.research.rename("sess-1", topic="renamed")
    args, kwargs = _last_call(m)
    assert args == ("PATCH", "/research/sessions/sess-1")
    assert kwargs["json"] == {"topic": "renamed"}


def test_research_delete(client):
    with patch.object(client._http, "request", return_value=_resp(200, {"deleted": True})) as m:
        client.research.delete("sess-1")
    args, _ = _last_call(m)
    assert args == ("DELETE", "/research/sessions/sess-1")


# ---------------------------------------------------------------------------
# Sync client.config()
# ---------------------------------------------------------------------------


def test_config_calls_get_config(client):
    payload = {"fields": [{"name": "log_level", "value": "INFO"}], "redacted": [], "count": 1}
    with patch.object(client._http, "request", return_value=_resp(200, payload)) as m:
        out = client.config()
    args, _ = _last_call(m)
    assert args == ("GET", "/config")
    assert out == payload


# ---------------------------------------------------------------------------
# Sync client.models.*
# ---------------------------------------------------------------------------


def test_models_list_filters_to_model_prefix(client):
    cfg = {"fields": [
        {"name": "log_level", "value": "INFO"},
        {"name": "model_general", "value": "qwen3:7b"},
        {"name": "model_coder", "value": "qwen2.5-coder:7b"},
        {"name": "milvus_uri", "value": "http://milvus:19530"},
    ], "count": 4}
    with patch.object(client._http, "request", return_value=_resp(200, cfg)) as m:
        out = client.models.list()
    args, _ = _last_call(m)
    assert args == ("GET", "/config")
    names = [f["name"] for f in out["fields"]]
    assert names == ["model_general", "model_coder"]
    assert out["count"] == 2


def test_models_list_handles_missing_fields(client):
    """Defensive: a /config response without fields shouldn't crash."""
    with patch.object(client._http, "request", return_value=_resp(200, {})) as _m:
        out = client.models.list()
    assert out == {"fields": [], "count": 0}


def test_models_available_extracts_loaded_list(client):
    health = {"checks": {"ollama": {"status": "up",
              "models_loaded": ["qwen3:4b", "qwen2.5:7b"]}}}
    with patch.object(client._http, "request", return_value=_resp(200, health)) as m:
        loaded = client.models.available()
    args, _ = _last_call(m)
    assert args == ("GET", "/health")
    assert loaded == ["qwen3:4b", "qwen2.5:7b"]


def test_models_available_returns_empty_when_ollama_down(client):
    health = {"checks": {"ollama": {"status": "down"}}}  # no models_loaded key
    with patch.object(client._http, "request", return_value=_resp(200, health)):
        loaded = client.models.available()
    assert loaded == []


def test_models_available_returns_empty_when_health_payload_malformed(client):
    """Response without `checks` (e.g., bare {} or non-dict) returns []."""
    with patch.object(client._http, "request", return_value=_resp(200, {})):
        assert client.models.available() == []


# ---------------------------------------------------------------------------
# Resource identity
# ---------------------------------------------------------------------------


def test_new_resources_have_stable_identity(client):
    assert client.research is client.research
    assert client.models is client.models


# ---------------------------------------------------------------------------
# Async parity
# ---------------------------------------------------------------------------


@pytest.fixture
async def aclient():
    async with AsyncClient("http://example.com", api_key="k") as c:
        yield c


async def test_async_research_list(aclient):
    with patch.object(aclient, "request", AsyncMock(return_value={"sessions": []})) as m:
        await aclient.research.list(q="k8s")
    args, kwargs = _last_call(m)
    assert args == ("GET", "/research/sessions")
    assert kwargs["params"]["q"] == "k8s"


async def test_async_research_rename(aclient):
    with patch.object(aclient, "request", AsyncMock(return_value={})) as m:
        await aclient.research.rename("sess-1", topic="new")
    args, kwargs = _last_call(m)
    assert args == ("PATCH", "/research/sessions/sess-1")
    assert kwargs["json"] == {"topic": "new"}


async def test_async_research_delete(aclient):
    with patch.object(aclient, "request", AsyncMock(return_value={"deleted": True})) as m:
        await aclient.research.delete("sess-1")
    args, _ = _last_call(m)
    assert args == ("DELETE", "/research/sessions/sess-1")


async def test_async_config(aclient):
    with patch.object(aclient, "request", AsyncMock(return_value={"fields": []})) as m:
        await aclient.config()
    args, _ = _last_call(m)
    assert args == ("GET", "/config")


async def test_async_models_list_filters(aclient):
    cfg = {"fields": [
        {"name": "model_general", "value": "x"},
        {"name": "log_level", "value": "INFO"},
    ]}
    with patch.object(aclient, "request", AsyncMock(return_value=cfg)):
        out = await aclient.models.list()
    assert [f["name"] for f in out["fields"]] == ["model_general"]


async def test_async_models_available(aclient):
    health = {"checks": {"ollama": {"models_loaded": ["qwen3:4b"]}}}
    with patch.object(aclient, "request", AsyncMock(return_value=health)):
        loaded = await aclient.models.available()
    assert loaded == ["qwen3:4b"]


async def test_async_new_resources_have_stable_identity(aclient):
    assert aclient.research is aclient.research
    assert aclient.models is aclient.models
