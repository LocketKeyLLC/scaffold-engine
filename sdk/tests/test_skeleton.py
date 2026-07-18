"""Skeleton tests for the SDK constructor + transport-error mapping.

Bigger surface (typed methods, streaming) lands in J.1.c / J.1.d. These
tests pin the behaviors the rest of the suite will rely on.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from scaffold_client import (
    AsyncClient,
    AuthenticationError,
    Client,
    ConflictError,
    ConnectionError,
    NotFoundError,
    OrchestratorError,
    PermissionError,
    RateLimitError,
    RequestError,
    ScaffoldError,
    TimeoutError,
    __version__,
)


def _resp(status: int, payload=None, text: str = "") -> MagicMock:
    r = MagicMock(spec=httpx.Response)
    r.status_code = status
    r.text = text
    r.json = MagicMock(return_value=payload if payload is not None else {})
    if payload is None:
        r.json.side_effect = ValueError("no body")
    return r


# -- Construction -----------------------------------------------------------


def test_version_is_exported():
    # Track the package's declared version rather than pinning a literal —
    # bumping in `_version.py` + `pyproject.toml` should not require a test
    # edit. Format check ensures we don't accidentally export a non-semver.
    parts = __version__.split(".")
    assert len(parts) >= 2 and all(p.isdigit() for p in parts[:2])


def test_x_api_key_header_set_when_key_given():
    with Client("http://example.com", api_key="abc123") as c:
        assert c._http.headers["x-api-key"] == "abc123"


def test_x_api_key_header_omitted_when_no_key():
    with Client("http://example.com") as c:
        assert "x-api-key" not in c._http.headers


def test_user_agent_includes_version():
    with Client("http://example.com") as c:
        assert __version__ in c._http.headers["user-agent"]


def test_base_url_trailing_slash_stripped():
    with Client("http://example.com/") as c:
        assert c.base_url == "http://example.com"


# -- Status code mapping ----------------------------------------------------


@pytest.mark.parametrize(
    "status, exc_cls",
    [
        (401, AuthenticationError),
        (403, PermissionError),
        (404, NotFoundError),
        (409, ConflictError),  # §17.607
        (422, RequestError),
        (429, RateLimitError),
        (500, OrchestratorError),
        (503, OrchestratorError),
    ],
)
def test_status_codes_raise_typed_error(status, exc_cls):
    with Client("http://example.com") as c:
        with patch.object(c._http, "request", return_value=_resp(status, {"detail": "boom"})):
            with pytest.raises(exc_cls):
                c.request("GET", "/whatever")


def test_typed_errors_inherit_from_scaffold_error():
    with Client("http://example.com") as c:
        with patch.object(c._http, "request", return_value=_resp(404, {"detail": "missing"})):
            with pytest.raises(ScaffoldError):
                c.request("GET", "/whatever")


def test_2xx_returns_parsed_json():
    with Client("http://example.com") as c:
        with patch.object(c._http, "request", return_value=_resp(200, {"ok": True})):
            assert c.request("GET", "/health") == {"ok": True}


def test_2xx_falls_back_to_text_on_invalid_json():
    with Client("http://example.com") as c:
        with patch.object(c._http, "request", return_value=_resp(200, payload=None, text="<html/>")):
            assert c.request("GET", "/health") == "<html/>"


# -- Transport-layer mapping ------------------------------------------------


def test_connect_error_translates_to_connection_error():
    with Client("http://example.com") as c:
        with patch.object(c._http, "request", side_effect=httpx.ConnectError("x")):
            with pytest.raises(ConnectionError):
                c.request("GET", "/health")


def test_timeout_translates_to_timeout_error():
    with Client("http://example.com") as c:
        with patch.object(c._http, "request", side_effect=httpx.ReadTimeout("x")):
            with pytest.raises(TimeoutError):
                c.request("GET", "/health")


def test_client_does_not_follow_redirects():
    """§17.421 — httpx does NOT strip the custom X-API-Key header on a
    cross-host 3xx, so following a redirect would leak the key. The sync
    Client must disable redirect-following."""
    with Client("http://example.com", api_key="secret") as c:
        assert c._http.follow_redirects is False


# -- AsyncClient mirror -----------------------------------------------------


async def test_async_client_404_raises_not_found_error():
    async with AsyncClient("http://example.com") as c:
        with patch.object(c._http, "request") as mock_req:
            mock_req.return_value = _resp(404, {"detail": "missing"})
            with pytest.raises(NotFoundError):
                await c.request("GET", "/jobs/abc")


async def test_async_client_2xx_returns_parsed_json():
    async with AsyncClient("http://example.com") as c:
        with patch.object(c._http, "request") as mock_req:
            mock_req.return_value = _resp(200, {"ok": True})
            assert await c.request("GET", "/health") == {"ok": True}


# -- Schemas re-export ------------------------------------------------------


def test_schemas_module_imports_and_has_central_types():
    from scaffold_client import schemas

    assert hasattr(schemas, "JobStatus")
    assert hasattr(schemas, "JOB_STATUSES")
    assert "completed" in schemas.JOB_STATUSES
