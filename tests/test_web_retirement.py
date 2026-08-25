"""§17.820 (plan 5.9) — /web is retired: every route 301s to its SPA home.

Replaces the rendering half of the deleted /web suites (test_web_ui,
test_web_pages, test_web_node_actions). The behavior half was ported to
API/SPA tests — see test_ideate_input_validation.py,
test_research_detached.py, test_job_phase.py, test_workflow_guards.py and
tests/ui/util.test.mjs.

No 404s from old bookmarks: every documented /web page maps to the SPA
route that superseded it; anything else (old form actions, SSE streams)
falls through to the SPA root.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.auth import require_api_key
from app.main import app


@pytest.fixture
def client():
    app.dependency_overrides[require_api_key] = lambda: "test"
    try:
        with TestClient(app, follow_redirects=False) as tc:
            yield tc
    finally:
        app.dependency_overrides.pop(require_api_key, None)


_REDIRECT_MAP = [
    ("/web/jobs", "/ui/#/"),
    ("/web/jobs/123e4567-e89b-12d3-a456-426614174000",
     "/ui/#/theater/123e4567-e89b-12d3-a456-426614174000"),
    ("/web/jobs/123e4567-e89b-12d3-a456-426614174000/fragment",
     "/ui/#/theater/123e4567-e89b-12d3-a456-426614174000"),
    ("/web/new", "/ui/#/new"),
    ("/web/rag", "/ui/#/rag"),
    ("/web/model", "/ui/#/models"),
    ("/web/research", "/ui/#/research"),
    ("/web/research/123e4567-e89b-12d3-a456-426614174000",
     "/ui/#/research/123e4567-e89b-12d3-a456-426614174000"),
]


class TestWebRedirects:
    @pytest.mark.parametrize("old,new", _REDIRECT_MAP)
    def test_page_redirects_to_spa_equivalent(self, client, old, new):
        resp = client.get(old)
        assert resp.status_code == 301
        assert resp.headers["location"] == new

    def test_catchall_lands_on_spa_root(self, client):
        resp = client.get("/web/anything/else/entirely")
        assert resp.status_code == 301
        assert resp.headers["location"] == "/ui/#/"

    def test_old_post_form_actions_redirect_not_404(self, client):
        """Old form actions (ideate/confirm/model/node verbs) must not 404 —
        they 301 to the SPA root; a following client GETs /ui/."""
        for old in ("/web/ideate", "/web/model", "/web/research",
                    "/web/jobs/x/confirm", "/web/jobs/x/nodes/T1/reset"):
            resp = client.post(old)
            assert resp.status_code == 301, old
            assert resp.headers["location"] == "/ui/#/"

    @pytest.mark.parametrize("multi_user", [False, True])
    def test_redirects_survive_missing_auth(self, monkeypatch, multi_user):
        """/web stays in _AUTH_EXEMPT_PREFIXES for the redirect release so
        old bookmarks land on the SPA login rather than a bare 401 — in BOTH
        auth modes (§17.820b dropped the §17.812 multi-user gate along with
        its reason: the routes are data-free 301s now)."""
        from app.config import settings

        monkeypatch.setattr(settings, "multi_user_enabled", multi_user)
        with TestClient(app, follow_redirects=False) as tc:
            resp = tc.get("/web/jobs")
        assert resp.status_code == 301


class TestRootRedirect:
    """GET / redirects to the standalone operator SPA at /ui/ (relocated
    verbatim from test_web_ui.py — the route never lived under /web)."""

    def test_root_redirects_to_ui(self, client):
        resp = client.get("/")
        assert resp.status_code == 302
        assert resp.headers["location"] == "/ui/"
