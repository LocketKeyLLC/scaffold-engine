"""§17.302 — error-recovery hints on stranded error paths.

Pre-§17.302 the scaffold_router error surface had uneven recovery
hints. Some errors (`/research` failure, `/idea` placeholder, the
welcome preamble) included a copy-pasteable next command; others
(404s on `/jobs rename/delete`, `/research/rename/delete`, `/results`,
`/skip`, `/schedule delete`; the orchestrator-unreachable banner)
stranded the operator with a status line and no path forward.

§17.302 sweeps the highest-impact gaps:

  - 8 "X not found" sites get a 💡 lookup hint pointing at the
    canonical list command (`/jobs`, `/research/list`,
    `/schedule list`)
  - 2 "Cannot reach orchestrator" sites get a 💡 `/health` hint

These tests pin each fix at its emission point.
"""
from unittest.mock import MagicMock, patch

import pytest
import requests

from tests._scaffold_router_setup import Pipeline


@pytest.fixture
def pipe():
    return Pipeline()


def _resp(status: int, body: dict | None = None) -> MagicMock:
    r = MagicMock(status_code=status, text="")
    r.json.return_value = body or {}
    return r


# ---------------------------------------------------------------------------
# "Job not found" — should suggest /jobs
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestJobNotFoundHints:

    def test_jobs_rename_404_suggests_jobs_list(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.patch") as mp:
            mp.return_value = _resp(404)
            out = pipe._jobs_rename_action("missing-id", "New Title")
        assert "Job not found" in out
        assert "`/jobs`" in out, (
            "§17.302: /jobs rename 404 must include `/jobs` discovery hint"
        )

    def test_jobs_delete_confirm_404_suggests_jobs_list(self, pipe):
        """Delete in CONFIRM mode: the operator already saw the preview
        and ran the confirm form. The 404 here means the job was
        deleted between preview and confirm — point at /jobs."""
        # Seed the pending-delete cache to bypass the 5-min preview gate.
        pipe._pending_deletes = {("job", "abcd1234"): __import__("time").time()}
        with patch("scaffold_router._HTTP_SESSION.delete") as md:
            md.return_value = _resp(404)
            out = pipe._jobs_delete_action("abcd1234", confirm=True)
        assert "Job not found" in out
        assert "`/jobs`" in out

    def test_jobs_delete_preview_404_suggests_jobs_list(self, pipe):
        """Preview path (no confirm) — the operator typed an unknown id."""
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _resp(404)
            out = pipe._jobs_delete_action("missing", confirm=False)
        assert "Job not found" in out
        assert "`/jobs`" in out

    def test_results_404_suggests_jobs_list(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _resp(404)
            out = pipe._handle_results(["/results", "missing-id"])
        assert "Job not found" in out
        assert "`/jobs`" in out

    def test_skip_candidates_404_suggests_jobs_list(self, pipe):
        """/skip <job_id> with no node_key lists candidate nodes for
        the job. If the job 404s, operator needs the /jobs hint."""
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _resp(404)
            out = pipe._render_skip_candidates("missing-id")
        assert "Job not found" in out
        assert "`/jobs`" in out


# ---------------------------------------------------------------------------
# "Research session not found" — should suggest /research/list
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestResearchSessionNotFoundHints:

    def test_research_rename_404_suggests_research_list(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.patch") as mp:
            mp.return_value = _resp(404)
            out = pipe._research_rename_action("missing-sid", "New Topic")
        assert "Research session not found" in out
        assert "`/research/list`" in out, (
            "§17.302: /research/rename 404 must include /research/list "
            "discovery hint"
        )

    def test_research_delete_confirm_404_suggests_research_list(self, pipe):
        import time
        pipe._pending_deletes = {("research", "abcd1234"): time.time()}
        with patch("scaffold_router._HTTP_SESSION.delete") as md:
            md.return_value = _resp(404)
            out = pipe._research_delete_action("abcd1234", confirm=True)
        assert "Research session not found" in out
        assert "`/research/list`" in out

    def test_research_delete_preview_session_missing_suggests_list(self, pipe):
        """Preview path: GET /research/sessions returns no matching id.
        Operator can't preview a delete on a session that doesn't
        exist — point them at /research/list."""
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _resp(200, {"sessions": []})  # empty list, no match
            out = pipe._research_delete_action("missing-sid", confirm=False)
        assert "Research session not found" in out
        assert "`/research/list`" in out


# ---------------------------------------------------------------------------
# "Schedule not found" — should suggest /schedule list
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestScheduleNotFoundHints:

    def test_schedule_delete_404_suggests_schedule_list(self, pipe):
        """/schedule delete <id> against a missing id should hint at
        the list command. Pre-§17.302 the operator saw only
        `❌ Schedule #N not found`."""
        with patch("scaffold_router._HTTP_SESSION.delete") as md:
            md.return_value = _resp(404)
            md.return_value.raise_for_status = MagicMock()
            out = pipe._handle_schedule("/schedule delete 99")
        assert "Schedule #99 not found" in out
        assert "`/schedule list`" in out, (
            "§17.302: /schedule delete 404 must include /schedule list "
            "discovery hint"
        )


# ---------------------------------------------------------------------------
# "Cannot reach orchestrator" — should suggest /health
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestOrchestratorUnreachableHints:

    def test_handle_command_connection_error_suggests_health(self, pipe):
        """Generic command path: any orchestrator HTTP call hitting a
        ConnectionError. _handle_command's outer except catches it."""
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.side_effect = requests.exceptions.ConnectionError("refused")
            out = pipe._handle_command("/status")
        assert "Cannot reach orchestrator" in out
        assert "`/health`" in out, (
            "§17.302: orchestrator-unreachable banner must point at "
            "/health for the diagnostic"
        )

    def test_results_connection_error_suggests_health(self, pipe):
        """/results has its own ConnectionError except branch."""
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.side_effect = requests.exceptions.ConnectionError("refused")
            out = pipe._handle_results(["/results", "abcd1234"])
        assert "Cannot reach orchestrator" in out
        assert "`/health`" in out


# ---------------------------------------------------------------------------
# Source-shape regression guard
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestSourceShapeRegressionGuard:
    """§17.302 — anchor the discovery-hint convention."""

    def _src(self) -> str:
        from pipelines import scaffold_router
        with open(scaffold_router.__file__, encoding="utf-8") as f:
            return f.read()

    def test_jobs_lookup_hint_anchored_for_not_found(self):
        """The phrase `list active jobs and copy a real job_id` must
        appear ≥ 5× (one per §17.302 job-not-found site: /jobs rename,
        /jobs delete confirm, /jobs delete preview, /results, /skip)."""
        src = self._src()
        count = src.count("list active jobs and copy a real job_id")
        assert count >= 5, (
            f"§17.302 regression: only {count} sites carry the "
            f"job-id lookup hint. Expected ≥ 5 — one per command "
            f"that 404s on a missing job_id."
        )

    def test_research_list_hint_anchored_for_not_found(self):
        """Three /research/* not-found sites (rename + delete confirm +
        delete preview) all share the same hint."""
        src = self._src()
        count = src.count("see active sessions and copy a real session_id")
        assert count >= 3, (
            f"§17.302 regression: only {count} /research/* sites "
            f"carry the session_id lookup hint. Expected ≥ 3."
        )

    def test_health_hint_anchored_for_unreachable(self):
        """The orchestrator-unreachable hint anchored on the diagnostic
        command name + the four subsystems probed."""
        src = self._src()
        assert "Postgres + Ollama + Milvus + Redis" in src, (
            "§17.302 regression: the orchestrator-unreachable hint "
            "no longer names the four subsystems probed by /health. "
            "The phrasing is the audit anchor — drift here breaks the "
            "operator-facing convention."
        )
