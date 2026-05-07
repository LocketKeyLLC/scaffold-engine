"""HTTP client wrapper — auth header injection + error translation."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from scaffold_cli.client import CLIError, Client


def _resp(status: int, payload=None, text: str = "") -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.text = text
    r.json = MagicMock(return_value=payload if payload is not None else {})
    if payload is None:
        r.json.side_effect = ValueError("no body")
    return r


def test_x_api_key_header_set_when_key_given():
    c = Client("http://example.com", api_key="abc123")
    headers = c._http.headers
    assert headers["X-API-Key"] == "abc123"
    c.close()


def test_x_api_key_header_omitted_when_no_key():
    c = Client("http://example.com", api_key=None)
    assert "X-API-Key" not in c._http.headers
    c.close()


def test_get_returns_parsed_json():
    c = Client("http://example.com", api_key=None)
    with patch.object(c._http, "request", return_value=_resp(200, {"ok": True})):
        assert c.get("/health") == {"ok": True}
    c.close()


def test_post_passes_json_body():
    c = Client("http://example.com", api_key=None)
    with patch.object(c._http, "request", return_value=_resp(200, {"id": "1"})) as m:
        c.post("/ideate", json={"idea": "hi"})
    args, kwargs = m.call_args
    assert kwargs.get("json") == {"idea": "hi"}
    assert args[0] == "POST"
    c.close()


def test_connect_error_becomes_friendly_cli_error():
    c = Client("http://nope.invalid", api_key=None)
    with patch.object(c._http, "request", side_effect=httpx.ConnectError("nope")):
        with pytest.raises(CLIError) as exc:
            c.get("/health")
    msg = str(exc.value)
    assert "Cannot reach orchestrator" in msg
    assert "make doctor" in msg
    c.close()


def test_timeout_becomes_friendly_cli_error():
    c = Client("http://example.com", api_key=None)
    with patch.object(c._http, "request", side_effect=httpx.TimeoutException("boom")):
        with pytest.raises(CLIError, match="timed out"):
            c.get("/jobs")
    c.close()


def test_401_message_points_at_doctor():
    c = Client("http://example.com", api_key="bad")
    with patch.object(
        c._http, "request",
        return_value=_resp(401, {"detail": "invalid"}, text="invalid"),
    ):
        with pytest.raises(CLIError) as exc:
            c.get("/jobs")
    msg = str(exc.value)
    assert "401" in msg
    assert "make doctor" in msg
    c.close()


def test_5xx_includes_detail_and_log_hint():
    c = Client("http://example.com", api_key=None)
    with patch.object(
        c._http, "request",
        return_value=_resp(500, {"detail": "boom"}, text="boom"),
    ):
        with pytest.raises(CLIError) as exc:
            c.post("/ideate", json={"idea": "x"})
    msg = str(exc.value)
    assert "500" in msg
    assert "boom" in msg
    assert "docker logs" in msg
    c.close()


def test_400_uses_detail_when_available():
    c = Client("http://example.com", api_key=None)
    with patch.object(
        c._http, "request",
        return_value=_resp(422, {"detail": "missing field 'idea'"}, text="x"),
    ):
        with pytest.raises(CLIError) as exc:
            c.post("/ideate", json={})
    assert "missing field" in str(exc.value)
    c.close()


def test_get_or_none_returns_none_on_404():
    c = Client("http://example.com", api_key=None)
    with patch.object(
        c._http, "request",
        return_value=_resp(404, {"detail": "not found"}, text="not found"),
    ):
        assert c.get_or_none("/jobs/missing") is None
    c.close()


def test_get_or_none_still_raises_on_other_errors():
    c = Client("http://example.com", api_key=None)
    with patch.object(
        c._http, "request",
        return_value=_resp(500, {"detail": "server died"}, text="x"),
    ):
        with pytest.raises(CLIError):
            c.get_or_none("/jobs/x")
    c.close()


def test_non_json_body_returns_text():
    """An endpoint that returns HTML or plain text shouldn't crash the
    client — the caller can format the raw text however it likes."""
    c = Client("http://example.com", api_key=None)
    fake = _resp(200, payload=None, text="<html>oops</html>")
    with patch.object(c._http, "request", return_value=fake):
        assert c.get("/something") == "<html>oops</html>"
    c.close()
