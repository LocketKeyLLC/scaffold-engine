"""Tests for app/auth.py (#9.19)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException


@pytest.fixture
def _api_key_set(monkeypatch):
    """Reload auth module with an API key set so tests are deterministic.

    Pydantic Settings is a singleton (instantiated at import of app.config),
    so we must reload BOTH app.config and app.auth — reloading auth alone
    leaves settings.scaffold_api_key pinned to the value at first import.
    """
    import importlib
    import app.config
    import app.auth
    monkeypatch.setenv("SCAFFOLD_API_KEY", "testkey123")
    importlib.reload(app.config)
    importlib.reload(app.auth)
    yield app.auth
    # Teardown: leave auth in a usable state for the NEXT test's import-time
    # check. Empty key + no opt-out raises RuntimeError at import, so we
    # always restore a known-good key here. monkeypatch will undo the env
    # changes, but the reloaded modules need to see something valid mid-frame.
    monkeypatch.setenv("SCAFFOLD_API_KEY", "testkey123")
    importlib.reload(app.config)
    importlib.reload(app.auth)


@pytest.fixture
def _api_key_unset(monkeypatch):
    """Reload auth with no key AND SCAFFOLD_AUTH_DISABLED=1 (the real dev opt-out).

    Empty key WITHOUT SCAFFOLD_AUTH_DISABLED=1 raises RuntimeError at module
    import — that's the documented contract in app/auth.py:11-15. The
    "permissive" mode is only reachable via the explicit opt-out flag.
    """
    import importlib
    import app.config
    import app.auth
    monkeypatch.delenv("SCAFFOLD_API_KEY", raising=False)
    monkeypatch.setenv("SCAFFOLD_AUTH_DISABLED", "1")
    importlib.reload(app.config)
    importlib.reload(app.auth)
    yield app.auth
    # Teardown: same reasoning as _api_key_set. Drop the opt-out flag,
    # restore a key, reload — leaves the module valid for the next import.
    monkeypatch.delenv("SCAFFOLD_AUTH_DISABLED", raising=False)
    monkeypatch.setenv("SCAFFOLD_API_KEY", "testkey123")
    importlib.reload(app.config)
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
async def test_explicit_auth_disabled_returns_empty(_api_key_unset):
    """SCAFFOLD_AUTH_DISABLED=1 with no key set bypasses auth (the dev opt-out).

    This is the only way to run without auth. Empty key alone raises
    RuntimeError at app.auth import time — see app/auth.py:11-15.
    """
    result = await _api_key_unset.require_api_key(_mk_request("/dag/abc"), key=None)
    assert result == ""
