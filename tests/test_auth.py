"""Tests for app/auth.py (#9.19)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException


@pytest.fixture
def _api_key_set(monkeypatch):
    """Reload auth module with an API key set so tests are deterministic."""
    import importlib
    monkeypatch.setenv("SCAFFOLD_API_KEY", "testkey123")
    import app.auth
    importlib.reload(app.auth)
    yield app.auth
    # Reset back without a key so subsequent tests don't inherit
    monkeypatch.delenv("SCAFFOLD_API_KEY", raising=False)
    importlib.reload(app.auth)


@pytest.fixture
def _api_key_unset(monkeypatch):
    """Reload auth with no key (auth disabled)."""
    import importlib
    monkeypatch.delenv("SCAFFOLD_API_KEY", raising=False)
    import app.auth
    importlib.reload(app.auth)
    yield app.auth
    importlib.reload(app.auth)


def _mk_request(path: str) -> MagicMock:
    req = MagicMock()
    req.url = MagicMock()
    req.url.path = path
    return req


@pytest.mark.smoke
async def test_health_path_passes_without_key(_api_key_set):
    """/health bypasses auth even when a key is set."""
    result = await _api_key_set.require_api_key(_mk_request("/health"), key=None)
    assert result == ""


@pytest.mark.smoke
async def test_valid_key_is_accepted(_api_key_set):
    result = await _api_key_set.require_api_key(_mk_request("/dag/abc"), key="testkey123")
    assert result == "testkey123"


@pytest.mark.smoke
async def test_missing_key_raises_401(_api_key_set):
    with pytest.raises(HTTPException) as exc_info:
        await _api_key_set.require_api_key(_mk_request("/dag/abc"), key=None)
    assert exc_info.value.status_code == 401


@pytest.mark.smoke
async def test_wrong_key_raises_401(_api_key_set):
    with pytest.raises(HTTPException) as exc_info:
        await _api_key_set.require_api_key(_mk_request("/dag/abc"), key="nope")
    assert exc_info.value.status_code == 401


@pytest.mark.smoke
async def test_no_key_configured_disables_auth(_api_key_unset):
    """Missing SCAFFOLD_API_KEY env means auth is permissive (dev fallback)."""
    result = await _api_key_unset.require_api_key(_mk_request("/dag/abc"), key=None)
    assert result == ""
