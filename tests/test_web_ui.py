"""Sprint J.2.a — read-only web UI tests.

Uses FastAPI TestClient against the live app, with the SDK Client
dependency overridden to inject canned payloads. No live orchestrator
or HTTP loopback needed.

Coverage:
  - GET / redirects to /web/jobs (302)
  - GET /web/jobs renders 200 + lists job titles in HTML
  - GET /web/jobs?status=completed surfaces the filter in the SDK call
  - GET /web/jobs/{id} renders 200 + shows status / counts / nodes
  - GET /web/jobs/{id} when SDK returns {error: ...} renders 404
  - GET /web/jobs validates limit / offset bounds
  - Static CSS is served at /static/web.css
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.auth import require_api_key
from app.main import app
from app.web.routes import get_sdk_client


@pytest.fixture
def fake_client():
    """Build a MagicMock SDK Client whose .jobs.list / .jobs.status are
    AsyncMock-free (the routes use the sync Client, not async)."""
    return MagicMock()


@pytest.fixture
def client(fake_client):
    """TestClient with auth bypassed (covers any non-web routes hit
    incidentally) AND get_sdk_client overridden so the routes never
    instantiate a real Client at module-load time."""
    app.dependency_overrides[require_api_key] = lambda: "test"
    app.dependency_overrides[get_sdk_client] = lambda: fake_client
    try:
        with TestClient(app, follow_redirects=False) as tc:
            yield tc
    finally:
        app.dependency_overrides.pop(require_api_key, None)
        app.dependency_overrides.pop(get_sdk_client, None)


@pytest.mark.smoke
class TestWebRootRedirect:
    """GET / should redirect to the web UI's jobs list."""

    def test_root_redirects_to_jobs(self, client):
        resp = client.get("/")
        assert resp.status_code == 302
        assert resp.headers["location"] == "/web/jobs"


@pytest.mark.smoke
class TestJobsListPage:
    """GET /web/jobs renders the list page."""

    def test_renders_jobs_table(self, client, fake_client):
        fake_client.jobs.list.return_value = {
            "jobs": [
                {"id": "j1", "title": "Build a homelab",
                 "status": "completed", "node_count": 5,
                 "created_at": "2026-05-08T00:00:00",
                 "updated_at": "2026-05-08T01:00:00"},
                {"id": "j2", "title": "Wire up RAG",
                 "status": "running", "node_count": 3,
                 "created_at": "2026-05-08T02:00:00",
                 "updated_at": "2026-05-08T03:00:00"},
            ],
            "total": 2, "limit": 25, "offset": 0,
        }
        resp = client.get("/web/jobs")
        assert resp.status_code == 200
        body = resp.text
        assert "Build a homelab" in body
        assert "Wire up RAG" in body
        # Both job-detail links present.
        assert "/web/jobs/j1" in body
        assert "/web/jobs/j2" in body
        # Status badges rendered with the correct status class.
        assert "status-completed" in body
        assert "status-running" in body

    def test_passes_status_filter_to_sdk(self, client, fake_client):
        fake_client.jobs.list.return_value = {
            "jobs": [], "total": 0, "limit": 25, "offset": 0,
        }
        client.get("/web/jobs?status=failed")
        fake_client.jobs.list.assert_called_once()
        # SDK called with status="failed" passed through.
        kwargs = fake_client.jobs.list.call_args.kwargs
        assert kwargs.get("status") == "failed"

    def test_empty_state_when_no_jobs(self, client, fake_client):
        fake_client.jobs.list.return_value = {
            "jobs": [], "total": 0, "limit": 25, "offset": 0,
        }
        resp = client.get("/web/jobs")
        assert resp.status_code == 200
        assert "No jobs match this filter" in resp.text

    def test_rejects_bad_limit(self, client, fake_client):
        resp = client.get("/web/jobs?limit=999")
        assert resp.status_code == 422

    def test_rejects_negative_offset(self, client, fake_client):
        resp = client.get("/web/jobs?offset=-1")
        assert resp.status_code == 422

    def test_sdk_failure_renders_error_page(self, client, fake_client):
        fake_client.jobs.list.side_effect = RuntimeError("orchestrator unreachable")
        resp = client.get("/web/jobs")
        assert resp.status_code == 502
        assert "Could not load jobs" in resp.text
        assert "orchestrator unreachable" in resp.text


@pytest.mark.smoke
class TestJobDetailPage:
    """GET /web/jobs/{id} renders the per-job detail."""

    def test_renders_status_and_nodes(self, client, fake_client):
        fake_client.jobs.status.return_value = {
            "job_id": "j1",
            "job_title": "Build a homelab",
            "job_status": "running",
            "compiled_output": None,
            "synthesized": False,
            "synthesis_override": None,
            "counts": {"done": 2, "pending": 1},
            "total_nodes": 3,
            "next_node": {"node_key": "T3", "title": "Final"},
            "next_actions": [],
            "nodes": [
                {"node_key": "T1", "title": "Plan", "status": "done",
                 "execution_order": 1, "actionable": False},
                {"node_key": "T2", "title": "Build", "status": "done",
                 "execution_order": 2, "actionable": False},
                {"node_key": "T3", "title": "Final", "status": "pending",
                 "execution_order": 3, "actionable": True},
            ],
        }
        resp = client.get("/web/jobs/j1")
        assert resp.status_code == 200
        body = resp.text
        assert "Build a homelab" in body
        assert "status-running" in body
        # All three node titles surfaced.
        assert "Plan" in body
        assert "Build" in body
        assert "Final" in body
        # Counts surfaced.
        assert "Total:" in body
        # Next-node row gets the highlighted class.
        assert "next-node" in body

    def test_compiled_output_rendered_in_pre_block(self, client, fake_client):
        fake_client.jobs.status.return_value = {
            "job_id": "j2",
            "job_title": "Wire up RAG",
            "job_status": "completed",
            "compiled_output": "# Project plan\n\n1. Step one\n2. Step two",
            "synthesized": True,
            "synthesis_override": True,
            "counts": {"done": 5},
            "total_nodes": 5,
            "next_node": None,
            "next_actions": [],
            "nodes": [],
        }
        resp = client.get("/web/jobs/j2")
        assert resp.status_code == 200
        body = resp.text
        # Output rendered (HTML-escaped — `#` becomes `#`, line breaks preserved in <pre>).
        assert "Step one" in body
        assert "Step two" in body
        # Synthesis flags surfaced.
        assert "Synthesis: forced on" in body
        assert "Last compile: synthesized" in body

    def test_missing_job_renders_404(self, client, fake_client):
        fake_client.jobs.status.return_value = {"error": "Job j-missing not found"}
        resp = client.get("/web/jobs/j-missing")
        assert resp.status_code == 404
        assert "not found" in resp.text.lower()

    def test_sdk_failure_renders_error_page(self, client, fake_client):
        fake_client.jobs.status.side_effect = ConnectionError("orchestrator down")
        resp = client.get("/web/jobs/j1")
        assert resp.status_code == 502
        assert "Could not load job j1" in resp.text


@pytest.mark.smoke
class TestStaticAssets:
    """The /static mount serves the web CSS without auth."""

    def test_static_css_served(self, client):
        resp = client.get("/static/web.css")
        assert resp.status_code == 200
        assert "text/css" in resp.headers["content-type"]
        # Sanity: a token from web.css is in the response body.
        assert "status-badge" in resp.text


@pytest.mark.smoke
class TestAuthBypass:
    """Web routes don't require an API key — they're meant to be browsed
    directly. The SDK Client carries the key for the loopback call."""

    def test_jobs_list_works_without_auth_header(self, fake_client):
        # Build a TestClient WITHOUT the require_api_key override —
        # confirms web routes truly bypass the global auth dependency.
        fake_client.jobs.list.return_value = {
            "jobs": [], "total": 0, "limit": 25, "offset": 0,
        }
        app.dependency_overrides[get_sdk_client] = lambda: fake_client
        try:
            with TestClient(app) as tc:
                resp = tc.get("/web/jobs")
            assert resp.status_code == 200
        finally:
            app.dependency_overrides.pop(get_sdk_client, None)
