"""§17.433 — unit tests for the code-runner sandbox client.

Offline: httpx.AsyncClient is faked. Covers the disabled short-circuit, the
success mapping, a sidecar-ran-but-code-failed verdict, transport failure,
and a malformed response. The sidecar server itself is proven via a live
build+run (docker/ is not mounted in the dev container).
"""
import pytest

from app import sandbox
from app.config import settings
from app.sandbox import client as cc

pytestmark = pytest.mark.smoke


class _FakeResp:
    def __init__(self, json_data):
        self._json = json_data

    def raise_for_status(self):
        return None

    def json(self):
        return self._json


class _FakeClient:
    """Async-context-manager stand-in for httpx.AsyncClient."""
    _resp = None
    _exc = None

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        if type(self)._exc is not None:
            raise type(self)._exc
        return type(self)._resp


@pytest.fixture
def fake_http(monkeypatch):
    monkeypatch.setattr(settings, "coderunner_url", "http://scaffold-coderunner:8010")

    def _set(resp=None, exc=None):
        _FakeClient._resp = resp
        _FakeClient._exc = exc
        monkeypatch.setattr(cc.httpx, "AsyncClient", _FakeClient)
    yield _set
    _FakeClient._resp = None
    _FakeClient._exc = None


@pytest.mark.asyncio
async def test_disabled_short_circuits_without_http(monkeypatch):
    monkeypatch.setattr(settings, "coderunner_url", "")

    class _Boom:
        def __init__(self, *a, **k):
            raise AssertionError("must not construct a client when disabled")

    monkeypatch.setattr(cc.httpx, "AsyncClient", _Boom)
    res = await sandbox.run_code({"m.py": "x=1"}, ["python", "m.py"])
    assert res.ok is False and res.error == "coderunner disabled"


@pytest.mark.asyncio
async def test_success_maps_fields(fake_http):
    fake_http(resp=_FakeResp({
        "ok": True, "exit_code": 0, "stdout": "2 passed",
        "stderr": "", "duration_ms": 42, "timed_out": False, "truncated": False,
    }))
    res = await sandbox.run_code({"t.py": "def test_x(): assert 1"}, ["pytest", "-q"])
    assert res.ok is True and res.exit_code == 0
    assert res.stdout == "2 passed" and res.duration_ms == 42 and res.error is None


@pytest.mark.asyncio
async def test_code_failed_is_data_not_error(fake_http):
    # Sidecar ran the code; it failed its tests. ok=False but error stays None.
    fake_http(resp=_FakeResp({
        "ok": False, "exit_code": 1, "stdout": "1 failed",
        "stderr": "AssertionError", "duration_ms": 10, "timed_out": False, "truncated": False,
    }))
    res = await sandbox.run_code({"t.py": "def test_x(): assert 0"}, ["pytest", "-q"])
    assert res.ok is False and res.exit_code == 1 and res.error is None


@pytest.mark.asyncio
async def test_transport_failure_is_fail_soft(fake_http):
    fake_http(exc=RuntimeError("connection refused"))
    res = await sandbox.run_code({"m.py": "x=1"}, ["python", "m.py"])
    assert res.ok is False and "connection refused" in res.error


@pytest.mark.asyncio
async def test_bad_shape_is_fail_soft(fake_http):
    fake_http(resp=_FakeResp({"unexpected": "shape"}))
    res = await sandbox.run_code({"m.py": "x=1"}, ["python", "m.py"])
    assert res.ok is False and "bad response shape" in res.error
