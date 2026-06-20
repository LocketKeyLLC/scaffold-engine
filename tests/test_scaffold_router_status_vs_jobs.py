"""§17.313 — `/status` vs `/jobs` disambiguation.

Both /status and /jobs surface "what jobs do I have?" but they
serve different operator intents:

  /status — at-a-glance dashboard: status counts + recent jobs +
            actionable Next-steps
  /jobs   — management list: filter / find / rename / delete

Pre-§17.313 the surfaces had no cross-reference. Operators landing
on /status (expecting filter/manage) had to know to switch to
/jobs; operators landing on /jobs (expecting an overview with
counts) had to know to switch to /status. Both were undiscoverable
from each other.

§17.313 adds:

  1. Cross-reference footer on /status pointing at /jobs for
     management actions.
  2. New 4th footer line on /jobs (§17.309's footer) pointing at
     /status for the at-a-glance overview.
  3. 📌 active-job marker on /status's recent-jobs table (synergy
     with §17.307 / §17.309; full-id match).
  4. Empty state on /status (when total=0) — surfaces the §17.300
     welcome's starter exemplars.

These tests pin: each new affordance + 📌 collision guard +
existing /status content preserved + source-shape regression
guards.
"""
from unittest.mock import MagicMock, patch

import pytest

from tests._scaffold_router_setup import Pipeline


@pytest.fixture
def pipe():
    return Pipeline()


_ACTIVE_JOB_ID = "abc1234e-d5f6-7890-abcd-ef1234567890"
_OTHER_JOB_ID = "ffff0000-1111-2222-3333-444455556666"
_CHAT_A = "chat-aaa-111"


def _status_payload(
    counts: dict | None = None,
    total: int = 0,
    recent: list | None = None,
) -> dict:
    return {
        "status_counts": counts or {},
        "total_jobs": total,
        "recent_jobs": recent or [],
    }


def _job(jid: str, title: str = "untitled", status: str = "running",
         updated_at: str = "2026-05-25T12:00:00", nodes: int = 5,
         next_actions: list | None = None) -> dict:
    return {
        "id": jid, "title": title, "status": status,
        "updated_at": updated_at, "node_count": nodes,
        "next_actions": next_actions or [],
    }


# ---------------------------------------------------------------------------
# /status cross-reference footer
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestStatusCrossRefFooter:

    def test_footer_present_on_populated_status(self, pipe):
        data = _status_payload(
            counts={"running": 2, "completed": 5},
            total=7,
            recent=[_job(_OTHER_JOB_ID)],
        )
        out = pipe._render_status(data, chat_id=_CHAT_A)
        # §17.562 — footer points at the core /here, and at /advanced for the
        # gated job-management commands.
        assert "`/here`" in out
        assert "rename / delete" in out
        assert "`/jobs`" in out

    def test_footer_appears_after_recent_table(self, pipe):
        data = _status_payload(
            counts={"running": 1}, total=1,
            recent=[_job(_OTHER_JOB_ID)],
        )
        out = pipe._render_status(data, chat_id=_CHAT_A)
        recent_idx = out.index("**Recent jobs")
        footer_idx = out.index("`/jobs`")
        assert recent_idx < footer_idx

    def test_footer_not_on_empty_state(self, pipe):
        """The empty state has its own starter block; the /jobs cross-
        ref would be contradictory (operator has no jobs to manage)."""
        out = pipe._render_status(_status_payload(), chat_id=_CHAT_A)
        # Empty state shape.
        assert "_No jobs yet._" in out
        # Cross-ref footer suppressed.
        assert "filter / find / rename / delete" not in out


# ---------------------------------------------------------------------------
# /status — 📌 active-job marker (synergy with §17.307)
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestStatusActiveJobMarker:

    def test_marker_prefixes_recalled_row(self, pipe):
        pipe._active_job_remember(_CHAT_A, _ACTIVE_JOB_ID, title="my job")
        data = _status_payload(
            counts={"running": 2}, total=2,
            recent=[
                _job(_ACTIVE_JOB_ID, title="my job"),
                _job(_OTHER_JOB_ID, title="other"),
            ],
        )
        out = pipe._render_status(data, chat_id=_CHAT_A)
        # Active job row prefixed with 📌.
        assert "📌 `abc1234e`" in out
        # Other row not prefixed.
        assert "📌 `ffff0000`" not in out

    def test_no_marker_when_no_recall(self, pipe):
        data = _status_payload(
            counts={"running": 1}, total=1,
            recent=[_job(_ACTIVE_JOB_ID)],
        )
        out = pipe._render_status(data, chat_id=_CHAT_A)
        assert "📌" not in out

    def test_no_marker_when_no_chat_id(self, pipe):
        """Curl-only callers — no recall, no marker."""
        pipe._active_job_remember(_CHAT_A, _ACTIVE_JOB_ID)
        data = _status_payload(
            counts={"running": 1}, total=1,
            recent=[_job(_ACTIVE_JOB_ID)],
        )
        out = pipe._render_status(data, chat_id=None)
        assert "📌" not in out

    def test_full_id_match_required(self, pipe):
        """Match on full id (not short id) — collision guard.
        Mirror of §17.309's full-id match contract."""
        pipe._active_job_remember(_CHAT_A, _ACTIVE_JOB_ID)
        # Different full id but POTENTIALLY similar short id —
        # share the first chars but not the full UUID.
        other_with_close_short = "abc1234f-9999-8888-7777-666655554444"
        data = _status_payload(
            counts={"running": 1}, total=1,
            recent=[_job(other_with_close_short)],
        )
        out = pipe._render_status(data, chat_id=_CHAT_A)
        # No 📌 — full id doesn't match.
        assert "📌" not in out


# ---------------------------------------------------------------------------
# /status empty state
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestStatusEmptyState:
    """§17.313 — when total=0 and no recent + no nonzero counts,
    /status surfaces the §17.300 welcome's starters."""

    def test_empty_state_header_and_message(self, pipe):
        out = pipe._render_status(_status_payload(), chat_id=_CHAT_A)
        assert "📊 Job Status" in out
        assert "_No jobs yet._" in out
        assert "Get started:" in out

    def test_empty_state_includes_idea_starter(self, pipe):
        """§17.300 welcome exemplar verbatim."""
        out = pipe._render_status(_status_payload(), chat_id=_CHAT_A)
        assert "/idea Build a CLI that converts screenshots to PDF" in out

    def test_empty_state_points_research_behind_advanced(self, pipe):
        # §17.562 — guided/minimal: the empty state no longer sends a brand-new
        # operator straight at gated /research; it surfaces /research behind the
        # /advanced pointer instead.
        out = pipe._render_status(_status_payload(), chat_id=_CHAT_A)
        assert "/advanced on" in out
        assert "/research" in out

    def test_empty_state_includes_chat_path(self, pipe):
        out = pipe._render_status(_status_payload(), chat_id=_CHAT_A)
        assert "describe an idea" in out
        assert "/go" in out

    def test_nonzero_counts_skip_empty_state(self, pipe):
        """A "completed: 5" counter alone (no recent visible because
        slice limit) should NOT trigger the empty state — operator has
        history, just nothing currently active."""
        data = _status_payload(
            counts={"completed": 5}, total=5, recent=[],
        )
        out = pipe._render_status(data, chat_id=_CHAT_A)
        assert "_No jobs yet._" not in out
        assert "Get started:" not in out
        # Counts table still rendered.
        assert "| completed | 5 |" in out


# ---------------------------------------------------------------------------
# /jobs footer — added /status reference (extension of §17.309)
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestJobsFooterIncludesStatus:
    """§17.313 — /jobs footer (§17.309) gets a 4th line cross-
    referencing /status."""

    def test_jobs_footer_mentions_status(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            r = MagicMock(status_code=200)
            r.json.return_value = {
                "jobs": [{"id": _OTHER_JOB_ID, "title": "x",
                          "status": "running", "node_count": 1,
                          "updated_at": "2026-05-25T12:00:00"}],
                "total": 1,
            }
            mg.return_value = r
            out = pipe._handle_jobs("/jobs", chat_id=_CHAT_A)
        # 4th footer line.
        assert "/status" in out
        # The existing §17.309 footer lines.
        assert "/results <id>" in out
        assert "/cost <id>" in out
        assert "/jobs help" in out


# ---------------------------------------------------------------------------
# Existing /status content preserved
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestStatusContentPreserved:
    """§17.313 changes are additive — pin the pre-§17.313 content
    (counts table, recent table, Next steps for actionable jobs)."""

    def test_counts_table_preserved(self, pipe):
        data = _status_payload(
            counts={"running": 2, "completed": 5}, total=7,
            recent=[_job(_OTHER_JOB_ID)],
        )
        out = pipe._render_status(data, chat_id=_CHAT_A)
        assert "| Status | Count |" in out
        # Sorted by count desc.
        completed_idx = out.index("| completed |")
        running_idx = out.index("| running |")
        assert completed_idx < running_idx

    def test_recent_table_preserved(self, pipe):
        data = _status_payload(
            counts={"running": 1}, total=1,
            recent=[_job(_OTHER_JOB_ID, title="my job")],
        )
        out = pipe._render_status(data, chat_id=_CHAT_A)
        assert "**Recent jobs" in out
        assert "my job" in out

    def test_actionable_next_steps_preserved(self, pipe):
        """Actionable jobs surface their orchestrator-provided
        next_actions block. §17.313 must not break this."""
        data = _status_payload(
            counts={"running": 1}, total=1,
            recent=[_job(
                _OTHER_JOB_ID, status="running",
                next_actions=[{
                    "action": "results",
                    "command": "/results ffff0000",
                    "description": "view progress",
                }],
            )],
        )
        out = pipe._render_status(data, chat_id=_CHAT_A)
        assert "**Next steps:**" in out
        assert "/results ffff0000" in out


# ---------------------------------------------------------------------------
# Source-shape regression guards
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestSourceShapeRegressionGuard:

    def _src(self) -> str:
        from pipelines import scaffold_router
        with open(scaffold_router.__file__, encoding="utf-8") as f:
            return f.read()

    def test_status_empty_state_helper_anchored(self):
        src = self._src()
        assert "def _status_empty_state" in src

    def test_render_status_accepts_chat_id(self, pipe):
        import inspect
        sig = inspect.signature(pipe._render_status)
        assert "chat_id" in sig.parameters

    def test_dispatch_passes_chat_id_to_render_status(self):
        src = self._src()
        assert "self._render_status(r.json(), chat_id=chat_id)" in src

    def test_status_cross_ref_anchored(self):
        """Pin the /status cross-ref phrasing — load-bearing for the
        disambiguation contract. §17.562 — points at /here + /advanced."""
        src = self._src()
        assert "unlocks job management" in src

    def test_jobs_footer_now_includes_status(self):
        """The §17.309 footer was extended in §17.313. Pin the
        4th line so a refactor that drops it trips review."""
        src = self._src()
        assert "/status` — at-a-glance dashboard" in src

    def test_status_marker_prefix_anchored(self):
        """The 📌 prefix block in _render_status — pin so a
        refactor that drops it regresses the §17.307 synergy."""
        src = self._src()
        assert "prefix = \"📌 \"" in src
