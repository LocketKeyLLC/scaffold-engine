"""Behavioral tests for app/utils/openapi_ingest."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


OPENAPI_3_MINIMAL = {
    "openapi": "3.0.0",
    "info": {"title": "Test API", "version": "1.0.0"},
    "paths": {
        "/users": {
            "get": {
                "operationId": "listUsers",
                "summary": "List users",
                "description": "Returns all users.",
                "tags": ["users"],
                "parameters": [
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                ],
                "responses": {"200": {"description": "OK"}},
            },
            "post": {
                "operationId": "createUser",
                "summary": "Create user",
                "tags": ["users"],
                "responses": {"201": {"description": "Created"}},
            },
        },
        "/users/{id}": {
            "parameters": [
                {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
            ],
            "get": {
                "operationId": "getUser",
                "tags": ["users"],
                "responses": {"200": {"description": "OK"}},
            },
        },
    },
}


SWAGGER_2_MINIMAL = {
    "swagger": "2.0",
    "info": {"title": "Legacy API", "version": "0.5.0"},
    "host": "api.example.com",
    "basePath": "/v1",
    "schemes": ["https"],
    "paths": {
        "/ping": {
            "get": {
                "operationId": "ping",
                "summary": "Health check",
                "tags": ["health"],
                "responses": {"200": {"description": "pong"}},
            },
        },
    },
}


def _mock_fetch_response(spec: dict):
    """httpx client returning given spec as JSON."""
    resp = MagicMock()
    resp.text = json.dumps(spec)
    resp.raise_for_status = MagicMock()

    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


@pytest.mark.asyncio
async def test_parse_openapi_3_happy_path():
    from app.utils import openapi_ingest

    with patch("app.utils.http_clients.get_generic_http_client", return_value=_mock_fetch_response(OPENAPI_3_MINIMAL)):
        entries, meta = await openapi_ingest.fetch_and_parse_spec("https://example.com/spec.json")

    assert meta["version"] == "openapi-3.0"
    assert meta["title"] == "Test API"
    assert meta["total_endpoints"] == 3
    assert meta["ingested_endpoints"] == 3
    assert meta["truncated"] is False

    paths_methods = {(e["path"], e["method"]) for e in entries}
    assert paths_methods == {("/users", "GET"), ("/users", "POST"), ("/users/{id}", "GET")}

    # Tags preserved
    for e in entries:
        assert "users" in e["tags"]


@pytest.mark.asyncio
async def test_parse_swagger_2_happy_path():
    from app.utils import openapi_ingest

    with patch("app.utils.http_clients.get_generic_http_client", return_value=_mock_fetch_response(SWAGGER_2_MINIMAL)):
        entries, meta = await openapi_ingest.fetch_and_parse_spec("https://example.com/swagger.json")

    assert meta["version"] == "swagger-2"
    assert meta["title"] == "Legacy API"
    assert len(entries) == 1
    assert entries[0]["path"] == "/ping"
    assert entries[0]["method"] == "GET"
    assert "health" in entries[0]["tags"]


@pytest.mark.asyncio
async def test_endpoint_cap_enforced(monkeypatch):
    from app.utils import openapi_ingest
    from app.config import settings

    monkeypatch.setattr(settings, "openapi_max_endpoints", 2)

    big_spec = {
        "openapi": "3.0.0",
        "info": {"title": "Big", "version": "1"},
        "paths": {
            f"/ep{i}": {"get": {"responses": {"200": {"description": "ok"}}}}
            for i in range(5)
        },
    }

    with patch("app.utils.http_clients.get_generic_http_client", return_value=_mock_fetch_response(big_spec)):
        entries, meta = await openapi_ingest.fetch_and_parse_spec("https://example.com/big.json")

    assert meta["total_endpoints"] == 5
    assert meta["ingested_endpoints"] == 2
    assert meta["truncated"] is True
    assert len(entries) == 2


@pytest.mark.asyncio
async def test_missing_version_field_raises():
    """Spec without 'openapi' or 'swagger' field → OpenAPIParseError."""
    from app.utils import openapi_ingest

    bad_spec = {"paths": {"/x": {"get": {}}}}  # no version marker

    with patch("app.utils.http_clients.get_generic_http_client", return_value=_mock_fetch_response(bad_spec)):
        with pytest.raises(openapi_ingest.OpenAPIParseError):
            await openapi_ingest.fetch_and_parse_spec("https://example.com/bad.json")


@pytest.mark.asyncio
async def test_validation_failure_raises():
    """Syntactically OK but schema-invalid spec → OpenAPIValidationError."""
    from app.utils import openapi_ingest

    # Missing required 'info' field for OpenAPI 3
    invalid_spec = {"openapi": "3.0.0", "paths": {}}

    with patch("app.utils.http_clients.get_generic_http_client", return_value=_mock_fetch_response(invalid_spec)):
        with pytest.raises(openapi_ingest.OpenAPIValidationError):
            await openapi_ingest.fetch_and_parse_spec("https://example.com/invalid.json")


@pytest.mark.asyncio
async def test_fetch_error_raises():
    """HTTP 404 on spec URL → OpenAPIFetchError."""
    import httpx
    from app.utils import openapi_ingest

    resp = MagicMock()
    resp.raise_for_status = MagicMock(side_effect=httpx.HTTPError("404"))

    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    with patch("app.utils.http_clients.get_generic_http_client", return_value=client):
        with pytest.raises(openapi_ingest.OpenAPIFetchError):
            await openapi_ingest.fetch_and_parse_spec("https://missing.example.com/spec.json")


def test_is_openapi_ref_matches_valid():
    from app.modules.research_agent import _is_openapi_ref
    assert _is_openapi_ref("openapi:https://api.example.com/openapi.json")
    assert _is_openapi_ref("openapi:http://localhost:8080/v3/api-docs")


def test_is_openapi_ref_rejects_invalid():
    from app.modules.research_agent import _is_openapi_ref
    assert not _is_openapi_ref("openapi:not-a-url")
    assert not _is_openapi_ref("openapi:ftp://example.com/spec.json")
    assert not _is_openapi_ref("https://example.com/openapi.json")
    assert not _is_openapi_ref("regular topic")
    assert not _is_openapi_ref("openapi:")


def test_parse_openapi_ref():
    from app.modules.research_agent import _parse_openapi_ref
    assert _parse_openapi_ref("openapi:https://api.example.com/spec.json") == "https://api.example.com/spec.json"
    assert _parse_openapi_ref("openapi:  https://x.com/y.json  ") == "https://x.com/y.json"
