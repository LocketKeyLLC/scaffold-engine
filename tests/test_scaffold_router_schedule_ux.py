"""§17.312 — `/schedule` listing UX.

Pre-§17.312 the /schedule surface had three rough spots:

  1. /schedule list with zero results returned a single-line
     "No schedules yet. Try ..." starter — terse, no --tz hint,
     no cron-shape variety.
  2. /schedule list (populated) returned a 7-column table with no
     next-actions footer — operators scanning saw rows but no
     copy-pasteable next command.
  3. /schedule help was a bullet list (5 lines) — no Examples
     section, no Flags section, --tz hidden in the parser help.

§17.312 adds three additive affordances (mirror of §17.309's
/jobs polish + §17.310's /research mode panel):

  - Richer empty state (3 cron flavors + tz tip)
  - Next-actions footer on populated list
  - Richer /schedule help (table + Examples + Flags)

These tests pin: empty state shows 3 starters + tz hint;
populated list has footer; help has table + Examples + Flags
sections; source-shape regression guards.
"""
from unittest.mock import MagicMock, patch

import pytest

from tests._scaffold_router_setup import Pipeline


@pytest.fixture
def pipe():
    return Pipeline()


def _list_response(schedules: list) -> MagicMock:
    r = MagicMock(status_code=200, text="")
    r.raise_for_status = MagicMock()
    r.json.return_value = {"schedules": schedules}
    return r


def _schedule(sid: int = 1, topic: str = "k8s news",
              cron: str = "0 9 * * 1", depth: str = "medium",
              next_run: str = "2026-06-01T09:00:00",
              runs: int = 0, failures: int = 0) -> dict:
    return {
        "id": sid, "topic": topic, "cron_expression": cron,
        "depth": depth, "next_run_at": next_run,
        "run_count": runs, "failure_count": failures,
    }


# ---------------------------------------------------------------------------
# Empty state on /schedule list
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestEmptyState:
    """§17.312 — /schedule list with zero results surfaces 3 cron
    flavors + the --tz tip (mirror of §17.309's /jobs empty state)."""

    def test_empty_state_header_present(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _list_response([])
            out = pipe._handle_schedule("/schedule list")
        # Match the §17.309 empty-state convention: header + sub-message
        # + starter block.
        assert "🗓 Schedules" in out
        assert "_No schedules yet._" in out
        assert "Get started" in out

    def test_three_cron_flavors_shown(self, pipe):
        """The three exemplars must teach distinct cron shapes:
        weekly UTC, daily UTC, and timezone-specific."""
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _list_response([])
            out = pipe._handle_schedule("/schedule list")
        # Weekly UTC.
        assert '/schedule add "0 9 * * 1" kubernetes news' in out
        # Daily UTC.
        assert '/schedule add "0 0 * * *" daily AI roundup' in out
        # Timezone-specific.
        assert '--tz=America/New_York' in out

    def test_empty_state_mentions_cron_format(self, pipe):
        """Cron syntax is non-obvious — empty state must teach the
        5-field shape inline (not just point at help)."""
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _list_response([])
            out = pipe._handle_schedule("/schedule list")
        # The 5 cron fields named in order.
        assert "minute hour day month weekday" in out

    def test_empty_state_points_at_help(self, pipe):
        """Reachable to the full reference."""
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _list_response([])
            out = pipe._handle_schedule("/schedule list")
        assert "/schedule help" in out


# ---------------------------------------------------------------------------
# Next-actions footer on populated list
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestNextActionsFooter:

    def test_footer_present_on_populated_list(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _list_response([_schedule()])
            out = pipe._handle_schedule("/schedule list")
        assert "💡 **Next:**" in out

    def test_footer_has_all_three_commands(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _list_response([_schedule()])
            out = pipe._handle_schedule("/schedule list")
        # The three next-actions match the §17.309 footer shape:
        # mutate, mutate (delete), discover (help).
        assert "/schedule add" in out
        assert "/schedule delete" in out
        assert "/schedule help" in out

    def test_footer_appears_below_table(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _list_response([_schedule()])
            out = pipe._handle_schedule("/schedule list")
        table_idx = out.index("| ID | Topic |")
        footer_idx = out.index("💡 **Next:**")
        assert table_idx < footer_idx

    def test_footer_absent_on_empty_state(self, pipe):
        """The empty state has its own starter block; the 💡 Next
        footer would be redundant and contradictory (no schedules
        to act on)."""
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _list_response([])
            out = pipe._handle_schedule("/schedule list")
        assert "💡 **Next:**" not in out


# ---------------------------------------------------------------------------
# Richer /schedule help
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestScheduleHelp:
    """§17.312 — /schedule help replaces the pre-§17.312 bullet list
    with a table + Examples + Flags sections (mirror of §17.310's
    /research mode panel)."""

    def test_help_uses_command_table(self, pipe):
        out = pipe._handle_schedule("/schedule help")
        # Table header column.
        assert "| Command | What it does |" in out

    def test_help_includes_examples_section(self, pipe):
        out = pipe._handle_schedule("/schedule help")
        assert "**Examples:**" in out
        # All three cron flavors (same set as the empty state).
        assert "Mondays at 9am UTC" in out
        assert "midnight UTC" in out
        assert "9am ET" in out

    def test_help_calls_out_tz_flag(self, pipe):
        """`--tz` is the highest-leverage hidden flag — pin that
        operators see it explicitly named (not just buried in an
        example)."""
        out = pipe._handle_schedule("/schedule help")
        assert "**Flags:**" in out
        assert "`--tz" in out

    def test_help_calls_out_depth_flag(self, pipe):
        out = pipe._handle_schedule("/schedule help")
        assert "`--depth" in out

    def test_help_explains_cron_format(self, pipe):
        out = pipe._handle_schedule("/schedule help")
        assert "minute hour day month weekday" in out

    def test_no_args_falls_through_to_help(self, pipe):
        """`/schedule` (no args) defaults to the same help text.
        Behavior preserved from pre-§17.312."""
        out = pipe._handle_schedule("/schedule")
        assert "Recurring research crons" in out


# ---------------------------------------------------------------------------
# Existing behavior preserved
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestExistingBehaviorPreserved:
    """§17.312 didn't change add / delete / unknown-subcommand
    branches. Pin a sample of those so a refactor doesn't drift."""

    def test_unknown_subcommand_still_suggests(self, pipe):
        out = pipe._handle_schedule("/schedule lst")
        # Pre-§17.312 close-match suggestion still works.
        assert "Unknown subcommand" in out

    def test_add_help_still_shows_parser_help(self, pipe):
        out = pipe._handle_schedule("/schedule add --help")
        # The /schedule add subcommand has its OWN help via parser;
        # pin that it still works (the §17.312 changes are scoped
        # to /schedule help, list empty, list footer).
        assert "schedule add" in out.lower()

    def test_delete_404_hint_preserved(self, pipe):
        """§17.302's existing /schedule delete 404 hint stays."""
        with patch("scaffold_router._HTTP_SESSION.delete") as md:
            r = MagicMock(status_code=404, text="")
            r.raise_for_status = MagicMock()
            md.return_value = r
            out = pipe._handle_schedule("/schedule delete 99")
        assert "Schedule #99 not found" in out
        assert "/schedule list" in out


# ---------------------------------------------------------------------------
# Source-shape regression guards
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestSourceShapeRegressionGuard:

    def _src(self) -> str:
        from pipelines import scaffold_router
        with open(scaffold_router.__file__, encoding="utf-8") as f:
            return f.read()

    def test_help_helper_anchored(self):
        src = self._src()
        assert "def _schedule_help" in src

    def test_empty_state_helper_anchored(self):
        src = self._src()
        assert "def _schedule_empty_state" in src

    def test_three_cron_exemplars_anchored(self):
        """The 3 cron flavors must appear in source (used by both
        empty state and help). Pin each literal."""
        src = self._src()
        assert "Mondays at 9am UTC" in src
        assert "midnight UTC" in src
        assert "9am ET" in src

    def test_footer_marker_anchored(self):
        src = self._src()
        # The footer's leading marker (matches §17.309's /jobs UX).
        assert '💡 **Next:**' in src

    def test_help_dispatch_routes_through_helper(self):
        """`if sub == "help": return self._schedule_help()` — pin
        the dispatch so a refactor that inlines the help text
        doesn't drop the helper-method boundary."""
        src = self._src()
        assert "return self._schedule_help()" in src

    def test_empty_dispatch_routes_through_helper(self):
        src = self._src()
        assert "return self._schedule_empty_state()" in src
