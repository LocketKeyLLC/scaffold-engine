"""§17.812 (audit C9) — WebCsrfMiddleware same-origin guard for /web.

The /web/* pages are auth-exempt (browsers carry no X-API-Key) yet drive
admin-privileged writes, so they were open to cross-origin CSRF. The middleware
refuses state-changing /web requests whose Origin/Referer host is cross-origin.

TestClient uses Host "testserver", so "http://testserver" is same-origin and
"http://evil.example" is cross-origin.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.middleware.web_csrf import WebCsrfMiddleware, _netloc_of


@pytest.fixture
def client():
    app = FastAPI()
    app.add_middleware(WebCsrfMiddleware)

    @app.post("/web/ideate")
    def web_ideate():
        return {"ok": True}

    @app.get("/web/page")
    def web_page():
        return {"ok": True}

    @app.post("/jobs")
    def jobs():
        return {"ok": True}

    return TestClient(app)


@pytest.mark.smoke
def test_cross_origin_web_post_rejected(client):
    r = client.post("/web/ideate", headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert "CSRF" in r.json()["detail"]


@pytest.mark.smoke
def test_same_origin_web_post_allowed(client):
    r = client.post("/web/ideate", headers={"Origin": "http://testserver"})
    assert r.status_code == 200


@pytest.mark.smoke
def test_web_post_without_origin_or_referer_allowed(client):
    # Non-browser tooling sends neither header; the localhost bind + normal auth
    # cover these. The CSRF vector requires a browser, which always sends Origin.
    r = client.post("/web/ideate")
    assert r.status_code == 200


@pytest.mark.smoke
def test_cross_origin_non_web_post_untouched(client):
    r = client.post("/jobs", headers={"Origin": "http://evil.example"})
    assert r.status_code == 200


@pytest.mark.smoke
def test_cross_origin_web_get_allowed(client):
    # GET is a safe method — never guarded.
    r = client.get("/web/page", headers={"Origin": "http://evil.example"})
    assert r.status_code == 200


@pytest.mark.smoke
def test_referer_fallback_when_no_origin(client):
    r = client.post("/web/ideate", headers={"Referer": "http://evil.example/x"})
    assert r.status_code == 403


@pytest.mark.smoke
def test_referer_same_origin_allowed(client):
    r = client.post("/web/ideate", headers={"Referer": "http://testserver/web/page"})
    assert r.status_code == 200


def test_netloc_of_helper():
    assert _netloc_of("http://evil.example") == "evil.example"
    assert _netloc_of("http://host:8000/a/b?q=1") == "host:8000"
    assert _netloc_of("") == ""
    assert _netloc_of("   ") == ""
