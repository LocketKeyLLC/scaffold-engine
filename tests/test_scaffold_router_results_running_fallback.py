"""§17.288 — `/results` against an in-progress job always renders a next step.

§17.280-UX-2 audit-tail concern: when the orchestrator's `/exec/status`
omits `next_actions` on a running/executing/planning/researching/refining
job, the chat reply rendered only the progress line — operator saw
"⏳ Status: running — 3/10 nodes complete" with no path forward.

§17.288 adds a fallback: if `_render_next_actions` returns empty for an
in-progress status, append a default "_No next steps suggested yet —
re-run `/results <job_id>` after the next node completes._" hint so
the operator always has a copy-pasteable next move.

These tests pin both directions of the contract:

  - non-empty next_actions → render those (no default appended)
  - empty/missing next_actions → render the §17.288 default hint
"""
from unittest.mock import MagicMock, patch

import pytest

from tests._scaffold_router_setup import Pipeline


@pytest.fixture
def pipe():
    return Pipeline()


def _running_status_response(*, next_actions=None) -> MagicMock:
    """Build a /exec/status response shaped like a mid-run job."""
    payload: dict = {
        "job_status": "running",
        "total_nodes": 5,
        "counts": {"done": 2, "running": 1, "pending": 2},
        "nodes": [
            {"node_key": "T1", "title": "step 1", "status": "done"},
            {"node_key": "T2", "title": "step 2", "status": "done"},
            {"node_key": "T3", "title": "step 3", "status": "running"},
            {"node_key": "T4", "title": "step 4", "status": "pending"},
            {"node_key": "T5", "title": "step 5", "status": "pending"},
        ],
    }
    if next_actions is not None:
        payload["next_actions"] = next_actions
    resp = MagicMock(status_code=200)
    resp.json.return_value = payload
    return resp


@pytest.mark.smoke
class TestRunningFallback:
    """§17.288 — empty next_actions on an in-progress status yields a
    default re-run hint instead of a dead-end progress line."""

    def test_missing_next_actions_emits_fallback_hint(self, pipe):
        """Orchestrator omits the field entirely (older orchestrator,
        or transient empty during a status flip) → fallback appears."""
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _running_status_response(next_actions=None)
            out = pipe._handle_results(["/results", "job-running-1"])
        # Progress head preserved.
        assert "running" in out
        assert "2/5" in out
        # §17.288 — the fallback wording is anchored so a future reword
        # surfaces in test review rather than silently shipping.
        assert "No next steps suggested yet" in out
        assert "`/results job-running-1`" in out
        assert "after the next node completes" in out

    def test_empty_next_actions_list_emits_fallback_hint(self, pipe):
        """Same fallback when the field IS present but empty — pre-§17.288
        the renderer's `_render_next_actions` returned "" on both cases
        and the result was identical (dead-end progress line). The
        fallback must cover both shapes for the audit to be closed.
        """
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _running_status_response(next_actions=[])
            out = pipe._handle_results(["/results", "job-running-2"])
        assert "No next steps suggested yet" in out

    def test_present_next_actions_skips_fallback(self, pipe):
        """When the orchestrator DID emit renderable actions, the
        fallback must NOT also appear — operators get the server-supplied
        actions, not a redundant copy-pasteable re-run hint on top.

        Note: ``_next_actions.format_block`` filters out
        ``{"action": "wait"}`` entries as noise (per §17.195's vendor
        helper). So a "renderable" action here means anything else with
        a ``command`` / ``endpoint`` / ``description`` field.
        """
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _running_status_response(next_actions=[
                {
                    "action": "skip",
                    "command": "/skip job-running-3 T3",
                    "description": "Skip the currently-running node",
                },
            ])
            out = pipe._handle_results(["/results", "job-running-3"])
        # The action-block content reached the user…
        assert "/skip job-running-3 T3" in out
        # …and the §17.288 fallback did NOT also fire.
        assert "No next steps suggested yet" not in out

    def test_only_wait_actions_treated_as_empty_and_fallback_fires(self, pipe):
        """``_next_actions.format_block`` filters out `"wait"` entries
        as noise. A next_actions list containing ONLY wait entries
        renders as empty — §17.288's fallback must fire here too,
        otherwise the operator sees a dead-end progress line whenever
        the orchestrator's only suggestion is "wait" (the common case
        for an actively-executing job).
        """
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _running_status_response(next_actions=[
                {"action": "wait", "description": "T3 is still executing"},
            ])
            out = pipe._handle_results(["/results", "job-running-4"])
        assert "No next steps suggested yet" in out

    @pytest.mark.parametrize(
        "status",
        ["running", "executing", "planning", "researching", "refining"],
    )
    def test_fallback_applies_to_every_in_progress_status(self, pipe, status):
        """The in-progress branch in `_handle_results` matches 5 statuses.
        Each must get the fallback when next_actions is empty."""
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            resp = MagicMock(status_code=200)
            resp.json.return_value = {
                "job_status": status,
                "total_nodes": 3,
                "counts": {"done": 1, "pending": 2},
                "nodes": [],
            }
            mg.return_value = resp
            out = pipe._handle_results(["/results", f"j-{status}"])
        assert status in out
        assert "No next steps suggested yet" in out

    def test_terminal_completed_does_not_emit_running_fallback(self, pipe):
        """The fallback string is scoped to the in-progress branch. A
        completed job must NOT pick it up — its branch renders compiled
        output and exits."""
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            resp = MagicMock(status_code=200)
            resp.json.return_value = {
                "status": "completed",
                "compiled_output": "## Final\n\nHello world.",
            }
            mg.return_value = resp
            out = pipe._handle_results(["/results", "j-done"])
        assert "Hello world" in out
        assert "No next steps suggested yet" not in out


@pytest.mark.smoke
class TestSourceShapeRegressionGuard:
    """§17.288 — anchor the fallback in the production source so a
    drive-by refactor that re-collapses the branch into a single
    `return head + self._render_next_actions(data)` line surfaces here.
    """

    def test_running_branch_has_actions_block_check(self):
        """Pin that the running branch reads `_render_next_actions` into
        a local and branches on truthiness. Pre-§17.288 the call was
        inlined as `return head + self._render_next_actions(data)` —
        the audit-fix shape requires the local variable + the if-guard.
        """
        from pipelines import scaffold_router

        with open(scaffold_router.__file__, encoding="utf-8") as f:
            src = f.read()

        # The fallback's anchor phrase must remain visible in source so
        # `git grep` from the audit entry lands on the right block.
        assert "No next steps suggested yet" in src, (
            "§17.288 regression: the empty-next_actions fallback wording "
            "has been removed from `_handle_results`. The in-progress "
            "branch must always provide a next-step hint, even when the "
            "orchestrator omits next_actions (older orchestrator or "
            "status-flip transient)."
        )
        assert "after the next node completes" in src, (
            "§17.288 regression: the operator-facing 'after the next "
            "node completes' suggestion has been removed or reworded "
            "in a way that breaks the audit anchor."
        )
