"""Audit top-5 guidance fixes (§17.508-512) — pipeline-side.

Covers the lifecycle-guidance gaps found in the 2026-06-14 full audit:
  §17.508 — hands-on (Shell) DAGs surface the autonomous-vs-assist choice
            instead of silently auto-executing.
  §17.509 — node_done ticker doesn't render "✅ complete" for unexecuted
            runbook (Shell) nodes.
  §17.512 — render_step flags a re-surfaced (already-presented) step.
(§17.510 research_complete reword + §17.511 summary anti-bleed are verified
 elsewhere — see test_research_summary_antibleed.py for §17.511.)
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from tests._scaffold_router_setup import Pipeline, _mod

# The vendored assist handlers are loaded by scaffold_router via _load_vendor
# (not importable as a package in the test env); reach them through the loaded
# module, the same way test_scaffold_router_assist_*.py does.
_ah = _mod._assist


@pytest.fixture
def pipe():
    return Pipeline()


@pytest.mark.smoke
class TestConfirmHandsOnChoice:
    """§17.508 — a DAG with Shell steps must STOP and offer the choice."""

    @patch("pipelines.scaffold_router._HTTP_SESSION.post")
    def test_shell_dag_offers_choice_not_autoexec(self, mock_post, pipe):
        confirm_resp = MagicMock(status_code=200)
        confirm_resp.json.return_value = {"status": "planning", "job_id": "job-hl"}
        dag_resp = MagicMock(status_code=200)
        dag_resp.json.return_value = {"task_count": 3, "tasks": [
            {"id": "T1", "tool": "LLM"},
            {"id": "T2", "tool": "Shell"},
            {"id": "T3", "tool": "Shell"}]}
        # No exec response queued: if the code tried /execute/all the
        # side_effect list would be exhausted and raise — a hard guard that
        # auto-execution did NOT happen.
        mock_post.side_effect = [confirm_resp, dag_resp]
        out = "".join(pipe.pipe(
            "/confirm job-hl", "m",
            [{"role": "user", "content": "/confirm job-hl"}], {}))
        urls = [c.args[0] for c in mock_post.call_args_list]
        assert not any("/execute/all" in u for u in urls), \
            "hands-on DAG must NOT auto-execute"
        assert "/assist job-hl" in out
        assert "/execute job-hl" in out
        assert "hands-on" in out.lower()

    @patch("pipelines.scaffold_router._HTTP_SESSION.post")
    def test_text_dag_still_autoexecutes(self, mock_post, pipe):
        confirm_resp = MagicMock(status_code=200)
        confirm_resp.json.return_value = {"status": "planning", "job_id": "job-txt"}
        dag_resp = MagicMock(status_code=200)
        dag_resp.json.return_value = {"task_count": 2, "tasks": [
            {"id": "T1", "tool": "LLM"}, {"id": "T2", "tool": "CodeGen"}]}
        exec_resp = MagicMock(status_code=200)
        exec_resp.iter_lines.return_value = iter([])
        exec_resp.close = MagicMock()
        mock_post.side_effect = [confirm_resp, dag_resp, exec_resp]
        list(pipe.pipe("/confirm job-txt", "m",
                       [{"role": "user", "content": "/confirm job-txt"}], {}))
        urls = [c.args[0] for c in mock_post.call_args_list]
        assert any("/execute/all" in u for u in urls), \
            "text-class DAG should still auto-execute"


@pytest.mark.smoke
class TestNodeDoneRunbookRender:
    """§17.509 — runbook_only nodes must not read as executed."""

    def test_runbook_only_node_not_marked_complete(self, pipe):
        out = "".join(pipe._handle_sse_event(
            "node_done", json.dumps({"node_key": "T2", "runbook_only": True}), []))
        assert "runbook generated" in out.lower()
        assert "✅" not in out

    def test_executed_node_marked_complete(self, pipe):
        out = "".join(pipe._handle_sse_event(
            "node_done", json.dumps({"node_key": "T1", "runbook_only": False}), []))
        assert "✅" in out
        assert "complete" in out.lower()


@pytest.mark.smoke
class TestRePresentedStepRender:
    """§17.512 — a re-surfaced presented step is flagged as the current step."""

    def _step(self, **extra):
        base = {"node_key": "T1", "title": "Install host", "tool": "Shell",
                "domain": "eng", "depends_on": [], "base_prompt": "do it",
                "upstream_outputs": {}}
        base.update(extra)
        return base

    def test_re_presented_shows_recovery_note(self):
        out = _ah.render_step(self._step(re_presented=True))
        assert "current step" in out.lower()
        assert "↩️" in out

    def test_fresh_step_has_no_recovery_note(self):
        out = _ah.render_step(self._step())
        assert "current step" not in out.lower()
