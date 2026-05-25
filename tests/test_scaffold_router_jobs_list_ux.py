"""§17.309 — `/jobs` default-listing UX.

Pre-§17.309 the `/jobs` listing was a bare table:

  ## 📋 Jobs — 25 of 47
  | Status | ID | Title | Nodes | Updated |
  ...

Empty results dropped to a terse `_No matching jobs._` with no
discoverable next step. Operators arriving from §17.300's welcome
(which surfaces /jobs as a starter) saw a wall of rows with no
hint about what to do with them.

§17.309 adds three UX affordances without altering the canonical
table shape:

  1. Empty state (unfiltered) — surfaces starter commands mirroring
     the §17.300 welcome. Empty state (filtered) — terse + suggests
     broadening.
  2. Next-actions footer — 3 copy-pasteable next commands after
     the table.
  3. 📌 active-job marker — uses §17.307's cache to prefix the row
     of the chat's most-recent job. Cross-cutting synergy.

These tests pin: empty unfiltered state shows starters; empty
filtered state stays terse + suggests broadening; footer present
on populated results; 📌 marker fires when §17.307 cache hits;
no marker when no recall; source-shape regression guards.
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


def _resp(jobs: list, total: int | None = None) -> MagicMock:
    """Mock the orchestrator /jobs response."""
    r = MagicMock(status_code=200, text="")
    r.json.return_value = {
        "jobs": jobs,
        "total": total if total is not None else len(jobs),
    }
    return r


def _job(jid: str, title: str = "untitled", status: str = "completed",
         updated_at: str = "2026-05-25T12:00:00", nodes: int = 5) -> dict:
    return {
        "id": jid, "title": title, "status": status,
        "updated_at": updated_at, "node_count": nodes,
    }


# ---------------------------------------------------------------------------
# Empty state (unfiltered)
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestEmptyStateUnfiltered:
    """§17.309 — `/jobs` with zero results surfaces starter commands."""

    def test_empty_state_shows_starter_commands(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _resp([], total=0)
            out = pipe._handle_jobs("/jobs", chat_id=_CHAT_A)
        # Header still present.
        assert "📋 Jobs" in out
        # The pre-§17.309 terse line + new "Get started" block.
        assert "_No jobs yet._" in out
        assert "Get started:" in out

    def test_empty_state_includes_idea_starter(self, pipe):
        """The /idea exemplar matches §17.300's welcome verbatim."""
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _resp([], total=0)
            out = pipe._handle_jobs("/jobs", chat_id=_CHAT_A)
        assert "/idea Build a CLI that converts screenshots to PDF" in out, (
            "§17.309 contract: the /idea starter exemplar must match "
            "§17.300's welcome verbatim so operators see consistent "
            "starters across discovery surfaces."
        )

    def test_empty_state_includes_research_starter(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _resp([], total=0)
            out = pipe._handle_jobs("/jobs", chat_id=_CHAT_A)
        assert "/research kubernetes best practices" in out

    def test_empty_state_includes_chat_path(self, pipe):
        """The chat → /go path is the canonical first-touch flow."""
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _resp([], total=0)
            out = pipe._handle_jobs("/jobs", chat_id=_CHAT_A)
        assert "describe an idea" in out
        assert "/go" in out


# ---------------------------------------------------------------------------
# Empty state (filtered)
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestEmptyStateFiltered:
    """§17.309 — filtered miss stays terse but suggests broadening."""

    def test_status_filter_empty_suggests_broadening(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _resp([], total=0)
            out = pipe._handle_jobs("/jobs failed", chat_id=_CHAT_A)
        # Header reflects filter.
        assert "filtered" in out
        # Suggestion to broaden.
        assert "/jobs find" in out or "no filter" in out.lower()

    def test_find_query_empty_suggests_broadening(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _resp([], total=0)
            out = pipe._handle_jobs(
                "/jobs find nonexistent", chat_id=_CHAT_A,
            )
        # Terse — does NOT show full "Get started" block (operator
        # already has context; just needs a tweak).
        assert "Get started:" not in out
        # But DOES suggest a broadening action.
        assert "_No matching jobs._" in out

    def test_filtered_empty_does_not_show_unfiltered_starters(self, pipe):
        """The chat → /go starter is for brand-new operators. A
        filtered miss means the operator IS active and just needs a
        wider net — don't re-onboard them."""
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _resp([], total=0)
            out = pipe._handle_jobs("/jobs failed", chat_id=_CHAT_A)
        assert "screenshots to PDF" not in out
        assert "describe an idea" not in out


# ---------------------------------------------------------------------------
# Next-actions footer (populated results)
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestNextActionsFooter:
    """§17.309 — populated /jobs results carry a footer with 3 next
    commands (mirror of the Next-block shape from §17.303 / §17.305)."""

    def test_footer_present_on_populated_results(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _resp([_job(_ACTIVE_JOB_ID)])
            out = pipe._handle_jobs("/jobs", chat_id=_CHAT_A)
        # The footer marker.
        assert "💡 **Next:**" in out
        # All 3 footer commands.
        assert "/results <id>" in out
        assert "/cost <id>" in out
        assert "/jobs help" in out

    def test_footer_below_table(self, pipe):
        """Footer placement: AFTER the table, never above (operators
        scan rows first, then look for next actions)."""
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _resp([_job(_ACTIVE_JOB_ID)])
            out = pipe._handle_jobs("/jobs", chat_id=_CHAT_A)
        # The table row appears before the footer in the string.
        table_idx = out.index("| Status |")
        footer_idx = out.index("💡 **Next:**")
        assert table_idx < footer_idx

    def test_footer_not_on_empty_state(self, pipe):
        """Empty-state surface is the "starter commands" block; the
        "💡 Next" footer would be redundant and contradictory there
        (no jobs to act on)."""
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _resp([], total=0)
            out = pipe._handle_jobs("/jobs", chat_id=_CHAT_A)
        assert "💡 **Next:**" not in out


# ---------------------------------------------------------------------------
# 📌 Active-job marker
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestActiveJobMarker:
    """§17.309 — when §17.307 has a recalled job that appears in the
    /jobs list, prefix its row with 📌."""

    def test_marker_appears_on_active_job_row(self, pipe):
        """The chat has a remembered /idea job; /jobs lists 3 jobs
        including it. The active job's row is prefixed with 📌."""
        pipe._active_job_remember(_CHAT_A, _ACTIVE_JOB_ID, title="my job")
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _resp([
                _job(_ACTIVE_JOB_ID, title="my job"),
                _job(_OTHER_JOB_ID, title="someone else's job"),
            ])
            out = pipe._handle_jobs("/jobs", chat_id=_CHAT_A)
        # 📌 prefix on the active-job row (left of the short id).
        assert "📌 `abc1234e`" in out
        # No 📌 on the non-active row.
        assert "📌 `ffff0000`" not in out

    def test_no_marker_when_no_recall(self, pipe):
        """Without an active-job memory entry, no row should carry 📌
        (mirror of §17.307's no-surprise contract)."""
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _resp([_job(_ACTIVE_JOB_ID)])
            out = pipe._handle_jobs("/jobs", chat_id=_CHAT_A)
        assert "📌" not in out

    def test_no_marker_when_chat_id_missing(self, pipe):
        """Curl-only callers (no chat_id) can't recall — no 📌."""
        pipe._active_job_remember(_CHAT_A, _ACTIVE_JOB_ID)
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _resp([_job(_ACTIVE_JOB_ID)])
            out = pipe._handle_jobs("/jobs", chat_id=None)
        assert "📌" not in out

    def test_marker_absent_when_active_job_not_in_list(self, pipe):
        """If the active job isn't in the displayed slice (e.g., past
        the 25-row limit), no row gets a 📌. Catches a bug where the
        marker would be applied to a wrong row by short-id collision."""
        pipe._active_job_remember(_CHAT_A, _ACTIVE_JOB_ID)
        # List contains a DIFFERENT job with a different full id but
        # POTENTIALLY similar short id — must still not match.
        other_with_close_id = "abc1234f-9999-8888-7777-666655554444"
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _resp([_job(other_with_close_id, title="other")])
            out = pipe._handle_jobs("/jobs", chat_id=_CHAT_A)
        # The 📌 must NOT appear — full-id match is required.
        assert "📌" not in out, (
            "§17.309: marker matched on a different job. The match "
            "MUST be on full id, not short id (collision risk)."
        )


# ---------------------------------------------------------------------------
# Filter behavior preserved (regression checks)
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestFilterBehaviorPreserved:
    """§17.309 didn't change the filter contract — pin that subcommands
    still flow through to the orchestrator with correct params."""

    def test_status_filter_in_query_params(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _resp([])
            pipe._handle_jobs("/jobs completed", chat_id=_CHAT_A)
        called_params = mg.call_args[1]["params"]
        assert called_params["status"] == "completed"

    def test_find_query_in_params(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _resp([])
            pipe._handle_jobs("/jobs find foo bar", chat_id=_CHAT_A)
        called_params = mg.call_args[1]["params"]
        assert called_params["q"] == "foo bar"


# ---------------------------------------------------------------------------
# Dispatch plumbing
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestDispatchPlumbing:

    def test_handle_jobs_accepts_chat_id(self, pipe):
        import inspect
        sig = inspect.signature(pipe._handle_jobs)
        assert "chat_id" in sig.parameters

    def test_jobs_list_action_accepts_chat_id(self, pipe):
        import inspect
        sig = inspect.signature(pipe._jobs_list_action)
        assert "chat_id" in sig.parameters

    def test_format_job_row_accepts_active_id(self, pipe):
        import inspect
        sig = inspect.signature(pipe._format_job_row)
        assert "active_id" in sig.parameters


# ---------------------------------------------------------------------------
# Source-shape regression guards
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestSourceShapeRegressionGuard:

    def _src(self) -> str:
        from pipelines import scaffold_router
        with open(scaffold_router.__file__, encoding="utf-8") as f:
            return f.read()

    def test_empty_state_helper_anchored(self):
        src = self._src()
        assert "def _jobs_empty_state" in src

    def test_starter_exemplars_anchored(self):
        """The /idea + /research starters in the empty state must
        match §17.300's welcome. Anchor verbatim so a drift on either
        side trips review."""
        src = self._src()
        # Same exemplars used in §17.300's _WELCOME_PREAMBLE.
        assert "screenshots to PDF" in src
        assert "kubernetes best practices" in src

    def test_footer_marker_anchored(self):
        src = self._src()
        assert "💡 **Next:**" in src

    def test_active_marker_anchored(self):
        """The 📌 prefix on the active-job row. Anchor the formatter
        line so a refactor that drops the prefix is visible."""
        src = self._src()
        assert "active_prefix = \"📌 \"" in src

    def test_pipe_passes_chat_id_to_handle_jobs(self):
        src = self._src()
        assert "self._handle_jobs(msg, chat_id=chat_id)" in src
