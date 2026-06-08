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
from app.web.routes import (
    _pipeline_steps,
    _render_markdown,
    get_sdk_async_long_client,
    get_sdk_client,
    get_sdk_long_client,
)


@pytest.fixture
def fake_client():
    """Build a MagicMock SDK Client whose .jobs.list / .jobs.status are
    AsyncMock-free (the routes use the sync Client, not async)."""
    return MagicMock()


@pytest.fixture
def fake_long_client():
    """Build a MagicMock SDK Client for the long-call routes (ideate /
    confirm). Distinct singleton from the read-path client so tests can
    assert calls against each independently."""
    return MagicMock()


@pytest.fixture
def fake_async_long_client():
    """Mock AsyncClient. ``aiter_execute_all`` is set per-test to an
    async-generator function so each case can drive the SSE stream."""
    return MagicMock()


@pytest.fixture
def client(fake_client, fake_long_client, fake_async_long_client):
    """TestClient with auth bypassed AND all three SDK Client deps
    overridden so the routes never instantiate real Clients at
    module-load time."""
    app.dependency_overrides[require_api_key] = lambda: "test"
    app.dependency_overrides[get_sdk_client] = lambda: fake_client
    app.dependency_overrides[get_sdk_long_client] = lambda: fake_long_client
    app.dependency_overrides[get_sdk_async_long_client] = (
        lambda: fake_async_long_client
    )
    try:
        with TestClient(app, follow_redirects=False) as tc:
            yield tc
    finally:
        app.dependency_overrides.pop(require_api_key, None)
        app.dependency_overrides.pop(get_sdk_client, None)
        app.dependency_overrides.pop(get_sdk_long_client, None)
        app.dependency_overrides.pop(get_sdk_async_long_client, None)


def _async_iter_factory(events: list):
    """Build a function that returns an async iterator yielding the
    given events. Tests assign this to ``fake_async_long_client.
    aiter_execute_all`` so the mock signature matches the real SDK."""
    async def _aiter(*args, **kwargs):
        for evt in events:
            yield evt
    return _aiter


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
        # §17.450 (B3) — no error banner / reason rows on a healthy job.
        assert "job-error-banner" not in body
        assert "node-reason" not in body

    def test_error_summary_banner_shown(self, client, fake_client):
        """§17.450 (B3) — a failed job surfaces error_summary as a banner."""
        fake_client.jobs.status.return_value = {
            "job_id": "jf", "job_title": "Broken job", "job_status": "failed",
            "compiled_output": None, "synthesized": False, "synthesis_override": None,
            "counts": {"failed": 1}, "total_nodes": 1, "next_node": None,
            "next_actions": [],
            "error_summary": "Job timed out after 1560 minutes of inactivity",
            "nodes": [{"node_key": "T1", "title": "x", "status": "failed",
                       "execution_order": 1, "actionable": False}],
        }
        body = client.get("/web/jobs/jf").text
        assert "job-error-banner" in body
        assert "Job timed out after 1560 minutes" in body

    def test_per_node_failure_reason_shown(self, client, fake_client):
        """§17.450 (B3) — a failed/blocked node surfaces its failure_reason."""
        fake_client.jobs.status.return_value = {
            "job_id": "jb", "job_title": "Blocked job", "job_status": "blocked",
            "compiled_output": None, "synthesized": False, "synthesis_override": None,
            "counts": {"failed": 1, "done": 1}, "total_nodes": 2, "next_node": None,
            "next_actions": [], "error_summary": None,
            "nodes": [
                {"node_key": "T1", "title": "ok", "status": "done",
                 "execution_order": 1, "actionable": False, "failure_reason": None},
                {"node_key": "T2", "title": "bad", "status": "failed",
                 "execution_order": 2, "actionable": False,
                 "failure_reason": "verifier: signature drift vs T1"},
            ],
        }
        body = client.get("/web/jobs/jb").text
        assert "node-reason" in body
        assert "signature drift vs T1" in body
        # error banner absent (no job-level error_summary)
        assert "job-error-banner" not in body

    def test_compiled_output_rendered_as_markdown(self, client, fake_client):
        """§17.458 — compiled_output renders as markdown (was a raw <pre> dump)."""
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
        # Rendered, not dumped: heading + ordered list, inside .markdown-body.
        assert "markdown-body" in body
        assert "<h1>Project plan</h1>" in body
        assert "<li>Step one</li>" in body
        assert "<pre class=\"output-body\">" not in body
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
class TestJobDetailLivePoll:
    """§17.455 — the detail page auto-polls only in transient wait states, and
    the fragment route re-emits / drops the trigger so polling self-stops."""

    @staticmethod
    def _payload(status):
        return {
            "job_id": "jp", "job_title": "Live job", "job_status": status,
            "compiled_output": None, "synthesized": False,
            "synthesis_override": None, "counts": {}, "total_nodes": 0,
            "next_node": None, "next_actions": [], "nodes": [],
        }

    def test_full_page_polls_in_transient_state(self, client, fake_client):
        fake_client.jobs.status.return_value = self._payload("refining")
        body = client.get("/web/jobs/jp").text
        assert 'id="job-detail-root"' in body
        assert 'hx-get="/web/jobs/jp/fragment"' in body
        assert 'hx-trigger="every 3s"' in body
        assert "live-indicator" in body

    def test_full_page_does_not_poll_in_interactive_state(self, client, fake_client):
        """awaiting_confirmation shows the confirm form — polling would wipe
        the user's in-progress feedback, so it must be OFF."""
        fake_client.jobs.status.return_value = self._payload("awaiting_confirmation")
        body = client.get("/web/jobs/jp").text
        assert 'id="job-detail-root"' in body
        assert "hx-trigger" not in body
        assert "live-indicator" not in body
        # The interactive element is present.
        assert "confirm-form" in body

    def test_fragment_route_returns_root_with_trigger_when_transient(
        self, client, fake_client,
    ):
        fake_client.jobs.status.return_value = self._payload("researching")
        resp = client.get("/web/jobs/jp/fragment")
        assert resp.status_code == 200
        body = resp.text
        assert 'id="job-detail-root"' in body
        assert 'hx-trigger="every 3s"' in body
        # Fragment carries the body too (not just the wrapper).
        assert "Node counts" in body

    def test_fragment_route_drops_trigger_at_terminal_state(self, client, fake_client):
        """When the job reaches completed, the swapped-in fragment has no
        trigger, so htmx stops polling on its own."""
        fake_client.jobs.status.return_value = self._payload("completed")
        body = client.get("/web/jobs/jp/fragment").text
        assert 'id="job-detail-root"' in body
        assert "hx-trigger" not in body

    def test_fragment_route_halts_on_error(self, client, fake_client):
        """A fetch error returns a bare non-polling section so the poll loop
        stops instead of hammering a broken backend every 3s."""
        fake_client.jobs.status.side_effect = ConnectionError("down")
        resp = client.get("/web/jobs/jp/fragment")
        assert resp.status_code == 200
        body = resp.text
        assert "hx-trigger" not in body
        assert "Lost contact" in body

    def test_fragment_route_halts_on_missing_job(self, client, fake_client):
        fake_client.jobs.status.return_value = {"error": "not found"}
        body = client.get("/web/jobs/jp/fragment").text
        assert "hx-trigger" not in body
        assert "Lost contact" in body


@pytest.mark.smoke
class TestJobDetailAutoRun:
    """§17.456 — ?run=1 auto-chains into execution at planning (chat-macro parity)."""

    @staticmethod
    def _payload(status):
        return {
            "job_id": "jp", "job_title": "Run job", "job_status": status,
            "compiled_output": None, "synthesized": False,
            "synthesis_override": None, "counts": {}, "total_nodes": 0,
            "next_node": None, "next_actions": [], "nodes": [],
        }

    def test_planning_with_run_auto_starts_stream(self, client, fake_client):
        fake_client.jobs.status.return_value = self._payload("planning")
        body = client.get("/web/jobs/jp?run=1").text
        # SSE stream auto-connects; the manual button is gone.
        assert 'sse-connect="/web/jobs/jp/run/stream"' in body
        assert "Run all nodes" not in body

    def test_planning_without_run_shows_manual_button(self, client, fake_client):
        fake_client.jobs.status.return_value = self._payload("planning")
        body = client.get("/web/jobs/jp").text
        assert "Run all nodes" in body
        assert 'sse-connect="/web/jobs/jp/run/stream"' not in body

    def test_executing_with_run_does_not_rekick(self, client, fake_client):
        """Auto-start is scoped to `planning`. An already-executing job keeps
        the manual button so we never re-POST /execute/all on a running job."""
        fake_client.jobs.status.return_value = self._payload("executing")
        body = client.get("/web/jobs/jp?run=1").text
        assert "Run all nodes" in body
        assert 'sse-connect="/web/jobs/jp/run/stream"' not in body

    def test_run_flag_survives_poll_while_researching(self, client, fake_client):
        """The poll target keeps ?run=1 so the flag isn't lost during the
        researching wait before planning is reached."""
        fake_client.jobs.status.return_value = self._payload("researching")
        body = client.get("/web/jobs/jp/fragment?run=1").text
        assert 'hx-get="/web/jobs/jp/fragment?run=1"' in body
        assert 'hx-trigger="every 3s"' in body

    def test_fragment_auto_starts_when_poll_lands_on_planning(self, client, fake_client):
        """The poll that catches the researching→planning transition swaps in a
        fragment that auto-connects the stream (polling has stopped by then)."""
        fake_client.jobs.status.return_value = self._payload("planning")
        body = client.get("/web/jobs/jp/fragment?run=1").text
        assert 'sse-connect="/web/jobs/jp/run/stream"' in body
        assert "hx-trigger" not in body  # planning is not transient → poll stops


@pytest.mark.smoke
class TestPipelineStepper:
    """§17.457 — lifecycle stepper mapping + render."""

    def _states(self, status, *, has_nodes=False):
        return {s["label"]: s["state"]
                for s in _pipeline_steps(status, has_nodes=has_nodes)}

    def test_refining_marks_first_step_current(self):
        st = self._states("refining")
        assert st["Refine"] == "current"
        assert st["Review"] == "upcoming"
        assert st["Done"] == "upcoming"

    def test_researching_marks_prior_done_and_research_current(self):
        st = self._states("researching")
        assert st["Refine"] == "done"
        assert st["Review"] == "done"
        assert st["Research"] == "current"
        assert st["Plan"] == "upcoming"

    def test_completed_marks_all_done(self):
        st = self._states("completed")
        assert set(st.values()) == {"done"}

    def test_failed_with_dag_errors_at_execute(self):
        st = self._states("failed", has_nodes=True)
        assert st["Execute"] == "error"
        assert st["Plan"] == "done"
        assert st["Done"] == "upcoming"

    def test_failed_without_dag_errors_early(self):
        st = self._states("failed", has_nodes=False)
        assert st["Refine"] == "error"
        assert st["Execute"] == "upcoming"

    def test_blocked_always_errors_at_execute(self):
        # blocked is an execution-phase state regardless of node presence.
        assert self._states("blocked", has_nodes=False)["Execute"] == "error"

    def test_rendered_page_shows_stepper_with_aria_current(self, client, fake_client):
        fake_client.jobs.status.return_value = {
            "job_id": "js", "job_title": "Stepper job", "job_status": "researching",
            "compiled_output": None, "synthesized": False, "synthesis_override": None,
            "counts": {}, "total_nodes": 0, "next_node": None, "next_actions": [],
            "nodes": [],
        }
        body = client.get("/web/jobs/js").text
        assert "pipeline-stepper" in body
        assert 'aria-current="step"' in body
        # Research is the current phase label.
        assert "Research" in body

    def test_stepper_updates_via_poll_fragment(self, client, fake_client):
        """The stepper recomputes on the §17.455 poll, so the fragment carries
        it too (not just the full page)."""
        fake_client.jobs.status.return_value = {
            "job_id": "js", "job_title": "x", "job_status": "refining",
            "compiled_output": None, "synthesized": False, "synthesis_override": None,
            "counts": {}, "total_nodes": 0, "next_node": None, "next_actions": [],
            "nodes": [],
        }
        body = client.get("/web/jobs/js/fragment").text
        assert "pipeline-stepper" in body


@pytest.mark.smoke
class TestMarkdownOutput:
    """§17.458 — server-side markdown rendering of compiled_output, XSS-safe."""

    def test_renders_common_markdown(self):
        html = _render_markdown("# H\n\n- a\n- b\n\n**bold** `code` [l](https://x.com)")
        assert "<h1>H</h1>" in html
        assert "<li>a</li>" in html
        assert "<strong>bold</strong>" in html
        assert "<code>code</code>" in html
        assert '<a href="https://x.com">l</a>' in html

    def test_fenced_code_block(self):
        html = _render_markdown("```python\nprint('hi')\n```")
        assert "<pre>" in html and "<code" in html and "print('hi')" in html

    def test_empty_and_none(self):
        assert _render_markdown(None) == ""
        assert _render_markdown("") == ""

    def test_raw_html_is_escaped_not_executed(self):
        html = _render_markdown("hi <script>alert(1)</script> <img src=x onerror=alert(1)>")
        assert "<script>" not in html
        assert "<img" not in html
        assert "&lt;script&gt;" in html  # escaped to inert text

    def test_javascript_link_is_neutralised(self):
        html = _render_markdown("[click](javascript:alert(1))")
        assert 'href="javascript:' not in html

    def test_rendered_output_marked_safe_in_page(self, client, fake_client):
        """The page must emit real tags (|safe), not escaped &lt;h1&gt;."""
        fake_client.jobs.status.return_value = {
            "job_id": "jm", "job_title": "x", "job_status": "completed",
            "compiled_output": "## Result\n\nDone.", "synthesized": False,
            "synthesis_override": None, "counts": {}, "total_nodes": 0,
            "next_node": None, "next_actions": [], "nodes": [],
        }
        body = client.get("/web/jobs/jm").text
        assert "<h2>Result</h2>" in body
        assert "&lt;h2&gt;" not in body


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
class TestAccessibility:
    """§17.461 — keyboard/skip-link/landmark/responsive-table a11y."""

    def test_skip_link_and_main_landmark(self, client, fake_client):
        fake_client.jobs.list.return_value = {
            "jobs": [], "total": 0, "limit": 25, "offset": 0,
        }
        body = client.get("/web/jobs").text
        assert 'class="skip-link" href="#main"' in body
        assert 'id="main"' in body
        assert 'aria-label="Primary"' in body  # nav landmark

    def test_jobs_table_wrapped_for_responsive_scroll(self, client, fake_client):
        fake_client.jobs.list.return_value = {
            "jobs": [{"id": "j1", "title": "t", "status": "completed",
                      "node_count": 1, "updated_at": "2026-06-08"}],
            "total": 1, "limit": 25, "offset": 0,
        }
        body = client.get("/web/jobs").text
        assert "table-scroll" in body

    def test_nodes_table_wrapped_for_responsive_scroll(self, client, fake_client):
        fake_client.jobs.status.return_value = {
            "job_id": "j1", "job_title": "x", "job_status": "running",
            "compiled_output": None, "synthesized": False, "synthesis_override": None,
            "counts": {}, "total_nodes": 1, "next_node": None, "next_actions": [],
            "nodes": [{"node_key": "T1", "title": "n", "status": "done",
                       "execution_order": 1, "actionable": False}],
        }
        body = client.get("/web/jobs/j1").text
        assert "table-scroll" in body


@pytest.mark.smoke
class TestVendoredAssets:
    """§17.459 — front-end libs are self-hosted, not loaded from a CDN."""

    def test_layout_references_local_vendor_not_cdn(self, client, fake_client):
        fake_client.jobs.list.return_value = {
            "jobs": [], "total": 0, "limit": 25, "offset": 0,
        }
        body = client.get("/web/jobs").text
        assert "/static/vendor/htmx-2.0.4.min.js" in body
        assert "/static/vendor/htmx-ext-sse-2.2.2.js" in body
        assert "unpkg.com" not in body

    def test_vendored_htmx_served_as_javascript(self, client):
        resp = client.get("/static/vendor/htmx-2.0.4.min.js")
        assert resp.status_code == 200
        assert "javascript" in resp.headers["content-type"]
        assert "htmx" in resp.text  # real lib, not an error page

    def test_vendored_sse_ext_served(self, client):
        resp = client.get("/static/vendor/htmx-ext-sse-2.2.2.js")
        assert resp.status_code == 200
        assert "javascript" in resp.headers["content-type"]
        assert "sse" in resp.text.lower()


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


# ---------------------------------------------------------------------------
# J.2.b — submit flow
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestNewIdeaForm:
    """GET /web/new renders the idea-submission form."""

    def test_renders_form_with_domain_options(self, client):
        resp = client.get("/web/new")
        assert resp.status_code == 200
        body = resp.text
        # Form posts to /web/ideate.
        assert 'action="/web/ideate"' in body
        # Textarea + domain select rendered.
        assert 'name="idea"' in body
        assert 'name="domain"' in body
        # Domain options match _ALLOWED_DOMAINS (§17.329 added eng_design).
        for d in ("prompt", "rag", "llm", "spec", "eng", "eng_design"):
            assert f'value="{d}"' in body


@pytest.mark.smoke
class TestPostIdeate:
    """§17.454 — POST /web/ideate calls /ideate/start and redirects to the
    LIVE job-detail page (was: BackgroundTask + redirect to refining filter)."""

    def test_redirects_to_job_detail(self, client, fake_client):
        fake_client.ideate_start.return_value = {
            "job_id": "job-xyz", "status": "refining",
        }
        resp = client.post(
            "/web/ideate",
            data={"idea": "Build a homelab dashboard", "domain": ""},
        )
        # 302 straight to the new job's detail page — no more hunting in a list.
        assert resp.status_code == 302
        assert resp.headers["location"] == "/web/jobs/job-xyz"

    def test_calls_ideate_start_with_form_values(self, client, fake_client):
        fake_client.ideate_start.return_value = {"job_id": "j1"}
        client.post(
            "/web/ideate",
            data={"idea": "Some idea", "domain": "rag"},
        )
        fake_client.ideate_start.assert_called_once()
        kwargs = fake_client.ideate_start.call_args.kwargs
        assert kwargs.get("idea") == "Some idea"
        assert kwargs.get("domain") == "rag"

    def test_empty_idea_re_renders_form_with_422(self, client, fake_client):
        resp = client.post(
            "/web/ideate", data={"idea": "   ", "domain": ""},
        )
        assert resp.status_code == 422
        assert "Idea is required" in resp.text
        # SDK was NOT called.
        fake_client.ideate_start.assert_not_called()

    def test_invalid_domain_re_renders_form_with_422(self, client, fake_client):
        resp = client.post(
            "/web/ideate",
            data={"idea": "Valid idea", "domain": "not-a-domain"},
        )
        assert resp.status_code == 422
        assert "Invalid domain" in resp.text
        fake_client.ideate_start.assert_not_called()

    def test_blank_domain_passes_none_to_sdk(self, client, fake_client):
        """The form's auto-detect option (value="") should resolve to
        domain=None at the SDK call site so the orchestrator infers."""
        fake_client.ideate_start.return_value = {"job_id": "j2"}
        client.post(
            "/web/ideate",
            data={"idea": "Auto-detect this", "domain": ""},
        )
        kwargs = fake_client.ideate_start.call_args.kwargs
        assert kwargs.get("domain") is None

    def test_missing_job_id_renders_error(self, client, fake_client):
        """If /ideate/start returns no job_id, surface a 502 error page
        rather than redirecting into a broken /web/jobs/None."""
        fake_client.ideate_start.return_value = {"status": "refining"}
        resp = client.post(
            "/web/ideate", data={"idea": "No id back", "domain": ""},
        )
        assert resp.status_code == 502
        assert "Could not submit idea" in resp.text


@pytest.mark.smoke
class TestPostConfirm:
    """POST /web/jobs/{id}/confirm kicks off Phase 2 in a BackgroundTask."""

    def test_redirects_to_job_detail_with_autorun(self, client, fake_long_client):
        """§17.456 — redirect carries ?run=1 so the page auto-starts execution
        once Phase 2 reaches planning (chat /confirm macro parity)."""
        resp = client.post(
            "/web/jobs/job-abc/confirm", data={"feedback": ""},
        )
        assert resp.status_code == 302
        assert resp.headers["location"] == "/web/jobs/job-abc?run=1"

    def test_calls_long_client_confirm_with_feedback(self, client, fake_long_client):
        client.post(
            "/web/jobs/job-abc/confirm",
            data={"feedback": "Focus on Python only."},
        )
        fake_long_client.confirm.assert_called_once()
        # confirm signature: (job_id, *, feedback, ...). Extract from args.
        call = fake_long_client.confirm.call_args
        assert call.args[0] == "job-abc" or call.kwargs.get("job_id") == "job-abc"
        assert call.kwargs.get("feedback") == "Focus on Python only."

    def test_blank_feedback_passes_none(self, client, fake_long_client):
        """Whitespace-only feedback should normalize to None so the
        SDK / orchestrator skip the no-op refinement path."""
        client.post(
            "/web/jobs/job-xyz/confirm",
            data={"feedback": "   "},
        )
        call = fake_long_client.confirm.call_args
        assert call.kwargs.get("feedback") is None


@pytest.mark.smoke
class TestConfirmFormVisibility:
    """The job-detail page shows the confirm form ONLY when status is
    awaiting_confirmation. Other statuses must not render it."""

    def test_form_shown_when_awaiting_confirmation(self, client, fake_client):
        fake_client.jobs.status.return_value = {
            "job_id": "j1", "job_title": "x",
            "job_status": "awaiting_confirmation",
            "compiled_output": None, "synthesized": False,
            "synthesis_override": None,
            "counts": {}, "total_nodes": 0,
            "next_node": None, "next_actions": [], "nodes": [],
        }
        resp = client.get("/web/jobs/j1")
        assert resp.status_code == 200
        assert 'action="/web/jobs/j1/confirm"' in resp.text
        assert "Confirm and run" in resp.text

    def test_form_hidden_when_running(self, client, fake_client):
        fake_client.jobs.status.return_value = {
            "job_id": "j2", "job_title": "x",
            "job_status": "running",
            "compiled_output": None, "synthesized": False,
            "synthesis_override": None,
            "counts": {}, "total_nodes": 0,
            "next_node": None, "next_actions": [], "nodes": [],
        }
        resp = client.get("/web/jobs/j2")
        assert resp.status_code == 200
        assert 'action="/web/jobs/j2/confirm"' not in resp.text

    def test_form_hidden_when_completed(self, client, fake_client):
        fake_client.jobs.status.return_value = {
            "job_id": "j3", "job_title": "x",
            "job_status": "completed",
            "compiled_output": "done", "synthesized": False,
            "synthesis_override": None,
            "counts": {}, "total_nodes": 0,
            "next_node": None, "next_actions": [], "nodes": [],
        }
        resp = client.get("/web/jobs/j3")
        assert 'action="/web/jobs/j3/confirm"' not in resp.text


# ---------------------------------------------------------------------------
# J.2.c — execute SSE
# ---------------------------------------------------------------------------


def _job_status_response(status: str) -> dict:
    return {
        "job_id": "j-run", "job_title": "demo", "job_status": status,
        "compiled_output": None, "synthesized": False,
        "synthesis_override": None,
        "counts": {}, "total_nodes": 0,
        "next_node": None, "next_actions": [], "nodes": [],
    }


@pytest.mark.smoke
class TestRunButtonVisibility:
    """The Run button only appears when the orchestrator's status allows
    /execute/all to make progress. (planning, executing, blocked)."""

    @pytest.mark.parametrize("status", ["planning", "executing", "blocked"])
    def test_button_shown_for_executable_statuses(self, client, fake_client, status):
        fake_client.jobs.status.return_value = _job_status_response(status)
        resp = client.get("/web/jobs/j-run")
        assert 'hx-post="/web/jobs/j-run/run"' in resp.text
        assert "Run all nodes" in resp.text

    @pytest.mark.parametrize(
        "status",
        ["awaiting_confirmation", "completed", "failed",
         "cancelled", "refining", "researching"],
    )
    def test_button_hidden_for_non_executable_statuses(self, client, fake_client, status):
        fake_client.jobs.status.return_value = _job_status_response(status)
        resp = client.get("/web/jobs/j-run")
        assert 'hx-post="/web/jobs/j-run/run"' not in resp.text


@pytest.mark.smoke
class TestPostRun:
    """POST /web/jobs/{id}/run returns the SSE-listening container fragment."""

    def test_returns_sse_container_html(self, client):
        resp = client.post("/web/jobs/j-run/run")
        assert resp.status_code == 200
        body = resp.text
        # Container has the HTMX SSE attributes the listening UL needs.
        assert 'hx-ext="sse"' in body
        assert 'sse-connect="/web/jobs/j-run/run/stream"' in body
        assert 'sse-swap="message"' in body
        assert 'hx-swap="beforeend"' in body
        # Container retains the run-section id so HTMX can swap it cleanly.
        assert 'id="run-section"' in body


@pytest.mark.smoke
class TestRunStream:
    """GET /web/jobs/{id}/run/stream proxies AsyncClient.aiter_execute_all
    events, rendering each as an HTML <li> wrapped in SSE message format."""

    def test_returns_event_stream_content_type(self, client, fake_async_long_client):
        fake_async_long_client.aiter_execute_all = _async_iter_factory([
            {"event": "pipeline_complete",
             "data": {"passed": 0, "failed": 0, "total_nodes": 0}},
        ])
        resp = client.get("/web/jobs/j-run/run/stream")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")

    def test_renders_node_done_event_as_li(self, client, fake_async_long_client):
        fake_async_long_client.aiter_execute_all = _async_iter_factory([
            {"event": "node_done",
             "data": {"node_key": "T1", "title": "Plan", "verified": True}},
            {"event": "pipeline_complete",
             "data": {"passed": 1, "failed": 0, "total_nodes": 1}},
        ])
        resp = client.get("/web/jobs/j-run/run/stream")
        body = resp.text
        # SSE message format: each event ends with blank line.
        assert "event: message" in body
        # The rendered <li> for node_done carries the key + title + verified flag.
        assert "run-event-done" in body
        assert "<code>T1</code>" in body
        assert "Plan" in body
        assert "verified" in body
        # Terminal event also rendered.
        assert "run-event-complete" in body

    def test_renders_node_failed_with_error(self, client, fake_async_long_client):
        fake_async_long_client.aiter_execute_all = _async_iter_factory([
            {"event": "node_failed",
             "data": {"node_key": "T2", "title": "Build",
                      "verification_reason": "tool not found"}},
            {"event": "pipeline_complete",
             "data": {"passed": 0, "failed": 1, "total_nodes": 1}},
        ])
        resp = client.get("/web/jobs/j-run/run/stream")
        body = resp.text
        assert "run-event-failed" in body
        assert "<code>T2</code>" in body
        # Error message surfaced in the fragment.
        assert "tool not found" in body

    def test_html_escapes_node_titles(self, client, fake_async_long_client):
        """Node titles can contain operator-supplied text. They must be
        HTML-escaped so a title like ``<script>`` doesn't punch through
        into the live SSE feed."""
        fake_async_long_client.aiter_execute_all = _async_iter_factory([
            {"event": "node_start",
             "data": {"node_key": "T1", "title": "<script>alert(1)</script>"}},
            {"event": "pipeline_complete", "data": {}},
        ])
        resp = client.get("/web/jobs/j-run/run/stream")
        body = resp.text
        # Raw <script> must NOT appear; the escaped form must.
        assert "<script>alert" not in body
        assert "&lt;script&gt;alert" in body

    def test_terminal_event_breaks_stream(self, client, fake_async_long_client):
        """After pipeline_complete, the route should stop iterating —
        events that come after the terminal must NOT appear."""
        fake_async_long_client.aiter_execute_all = _async_iter_factory([
            {"event": "node_done",
             "data": {"node_key": "T1", "title": "First"}},
            {"event": "pipeline_complete", "data": {"passed": 1, "failed": 0}},
            # These should NOT be rendered — generator returns on terminal.
            {"event": "node_done",
             "data": {"node_key": "T-ghost", "title": "After-Terminal"}},
        ])
        resp = client.get("/web/jobs/j-run/run/stream")
        body = resp.text
        assert "First" in body
        assert "After-Terminal" not in body
        assert "T-ghost" not in body

    def test_async_iter_failure_renders_error_event(self, client, fake_async_long_client):
        """If the AsyncClient's iterator itself raises, the route catches
        the exception and emits a final error event so the UI doesn't
        silently freeze."""
        async def _raising(*args, **kwargs):
            yield {"event": "node_done",
                   "data": {"node_key": "T1", "title": "First"}}
            raise RuntimeError("orchestrator died mid-stream")

        fake_async_long_client.aiter_execute_all = _raising
        resp = client.get("/web/jobs/j-run/run/stream")
        body = resp.text
        # The earlier event made it through.
        assert "First" in body
        # The error fragment was emitted.
        assert "run-event-error" in body
        assert "orchestrator died mid-stream" in body

    def test_unknown_event_passes_through(self, client, fake_async_long_client):
        """Unknown event names get a minimal 'other' rendering so SDK-side
        additions surface to operators rather than being silently dropped."""
        fake_async_long_client.aiter_execute_all = _async_iter_factory([
            {"event": "future_event_kind", "data": {"foo": "bar"}},
            {"event": "pipeline_complete", "data": {}},
        ])
        resp = client.get("/web/jobs/j-run/run/stream")
        body = resp.text
        assert "run-event-other" in body
        assert "future_event_kind" in body


@pytest.mark.smoke
class TestSseExtensionLoaded:
    """Layout templates load the htmx-ext-sse script so the SSE container
    fragments returned by /run actually function in the browser."""

    def test_sse_extension_in_layout(self, client, fake_client):
        fake_client.jobs.status.return_value = _job_status_response("planning")
        resp = client.get("/web/jobs/j-run")
        assert "htmx-ext-sse" in resp.text
