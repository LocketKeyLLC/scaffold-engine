"""§17.310 — `/research` mode discovery.

Pre-§17.310 `/research` (no args) and `/research --help` both dumped
the parser's plain `help_text()`: 4 example lines with no purpose
column. Operators arriving at /research from the §17.300 welcome
(which surfaces `/research kubernetes best practices` as a starter)
saw the parser dump but didn't learn WHEN to reach for the
`github:` / `openapi:` / URL / PDF modes.

§17.310 introduces `_research_modes_panel()` — a 5-row table
(Topic / URL / GitHub / OpenAPI / PDF) with a "When to use" column
plus an example column. Both the no-args path AND the --help path
route through it.

The disambiguation prompt for short-looking queries also gains a
1-line mode hint pointing at `/research --help` so operators who
typed a topic but actually had a URL or repo handy discover the
modes.

These tests pin: all 5 modes present in the panel; no-args path
uses the panel; --help path uses the panel; disambig prompt
mentions modes; source-shape regression guards.
"""
from unittest.mock import patch

import pytest

from tests._scaffold_router_setup import Pipeline


@pytest.fixture
def pipe():
    return Pipeline()


def _drive_research(pipe, msg: str) -> str:
    """Run pipe._handle_research and collect its output."""
    return "".join(pipe._handle_research(msg))


# ---------------------------------------------------------------------------
# Mode panel contents
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestModePanelContents:
    """§17.310 — all 5 modes must appear with purpose + example."""

    def test_all_five_modes_named(self, pipe):
        panel = pipe._research_modes_panel()
        for mode_label in ("Topic", "URL", "GitHub", "OpenAPI", "PDF"):
            assert f"**{mode_label}**" in panel, (
                f"§17.310: mode row `{mode_label}` missing from panel"
            )

    def test_each_mode_has_example(self, pipe):
        panel = pipe._research_modes_panel()
        # Each example exemplar must be present verbatim (anchor with
        # backticks since the table shows them in code-format).
        for example in (
            "`/research kubernetes best practices`",
            "`/research https://example.com/article`",
            "`/research github:owner/repo`",
            "`/research openapi:https://api.example.com/openapi.json`",
            "`/research/pdf`",
        ):
            assert example in panel, (
                f"§17.310: example `{example}` missing from panel"
            )

    def test_when_to_use_column_present(self, pipe):
        """The differentiator from the pre-§17.310 plain help_text — a
        column that teaches WHEN to reach for each mode."""
        panel = pipe._research_modes_panel()
        assert "When to use" in panel

    def test_purpose_text_meaningful_per_mode(self, pipe):
        """Each row must teach something. Pin a phrase per mode so a
        future refactor that collapses descriptions to "see help" is
        visible at review."""
        panel = pipe._research_modes_panel()
        # Topic: open-ended discovery.
        assert "Open-ended" in panel or "discover sources" in panel
        # URL: specific page.
        assert "Specific page" in panel or "ingested verbatim" in panel
        # GitHub: repo content.
        assert "Repo" in panel and "README" in panel
        # OpenAPI: per-endpoint.
        assert "per endpoint" in panel
        # PDF: drag-drop / curl.
        assert "drag-drop" in panel.lower() or "curl" in panel

    def test_flags_section_present(self, pipe):
        panel = pipe._research_modes_panel()
        assert "--depth" in panel
        # Confirm flag for scripted callers.
        assert "--confirm" in panel

    def test_management_pointer_present(self, pipe):
        """The panel must point at /research/help for management
        subcommands (list / find / rename / delete) so operators don't
        confuse the two surfaces."""
        panel = pipe._research_modes_panel()
        assert "/research/help" in panel


# ---------------------------------------------------------------------------
# Entry paths route through the panel
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestEntryPathsUsePanel:
    """§17.310 — `/research` (no args) AND `/research --help` both
    surface the panel."""

    def test_no_args_uses_panel(self, pipe):
        out = _drive_research(pipe, "/research")
        # Panel signature lines.
        assert "Pick a mode based on what you have:" in out
        # All 5 modes.
        assert "**GitHub**" in out
        assert "**OpenAPI**" in out

    def test_dash_dash_help_uses_panel(self, pipe):
        out = _drive_research(pipe, "/research --help")
        assert "Pick a mode based on what you have:" in out

    def test_dash_h_uses_panel(self, pipe):
        out = _drive_research(pipe, "/research -h")
        assert "Pick a mode based on what you have:" in out

    def test_help_word_uses_panel(self, pipe):
        out = _drive_research(pipe, "/research help")
        assert "Pick a mode based on what you have:" in out

    def test_no_args_does_not_fall_through_to_placeholder(self, pipe):
        """Pre-§17.310 the no-args case fell through to the
        `_is_placeholder("")` branch ("topic is missing or a
        placeholder"). Post-§17.310 the panel short-circuits the
        function — the placeholder line must NOT appear."""
        out = _drive_research(pipe, "/research")
        assert "topic is missing or a placeholder" not in out


# ---------------------------------------------------------------------------
# Disambiguation prompt — mode hint surfaced
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestDisambigPromptModeHint:
    """§17.310 — short-query disambig prompt now nudges operators
    toward modes if they had a URL / repo / spec but typed a topic."""

    def test_disambig_prompt_includes_mode_hint(self, pipe):
        # Short query triggers the rag-or-research disambig prompt.
        out = _drive_research(pipe, "/research how does k8s work")
        # Existing /rag vs /research --confirm options.
        assert "/rag" in out
        assert "--confirm" in out
        # §17.310 — new mode hint.
        assert "github:owner/repo" in out
        assert "openapi:" in out

    def test_disambig_hint_points_at_research_help(self, pipe):
        """The hint sends operators to /research --help for the full
        mode table (not a re-dump inline)."""
        out = _drive_research(pipe, "/research short query")
        assert "/research --help" in out

    def test_long_query_skips_disambig_and_mode_hint(self, pipe):
        """The disambig prompt only fires for queries that LOOK LIKE
        /rag queries (≤ 4 tokens). Long queries route directly to
        research — the disambig + mode hint must NOT appear."""
        with patch.object(
            pipe, "_research_and_stream", return_value=iter(["streaming"]),
        ):
            out = _drive_research(
                pipe,
                "/research how does the kubernetes scheduler interact "
                "with kubelet to schedule pods across many nodes",
            )
        # Disambig prompt phrases not present.
        assert "did you mean `/rag`?" not in out

    def test_url_query_skips_disambig_and_mode_hint(self, pipe):
        """`/research <url>` is unambiguously research-mode input;
        no disambig prompt, no mode hint inline."""
        with patch.object(
            pipe, "_research_and_stream", return_value=iter(["streaming"]),
        ):
            out = _drive_research(
                pipe, "/research https://example.com/article",
            )
        assert "did you mean `/rag`?" not in out


# ---------------------------------------------------------------------------
# Source-shape regression guards
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestSourceShapeRegressionGuard:

    def _src(self) -> str:
        from pipelines import scaffold_router
        with open(scaffold_router.__file__, encoding="utf-8") as f:
            return f.read()

    def test_panel_helper_anchored(self):
        src = self._src()
        assert "def _research_modes_panel" in src

    def test_no_args_routes_through_panel(self):
        """The no-args path must NOT fall through to placeholder
        rejection. Pin the in-tuple branch."""
        src = self._src()
        assert 'in ("", "--help", "-h", "help")' in src

    def test_disambig_mentions_modes(self):
        """Pin the mode-hint phrasing in the disambig prompt source."""
        src = self._src()
        assert "github:owner/repo" in src
        # The /research --help pointer (not a re-dump inline).
        assert "See `/research --help`" in src

    def test_all_five_mode_rows_anchored_in_panel(self):
        """Pin each mode-label literal in scaffold_router.py so a
        refactor that collapses rows trips review."""
        src = self._src()
        for label in ("**Topic**", "**URL**", "**GitHub**", "**OpenAPI**", "**PDF**"):
            assert label in src, (
                f"§17.310 regression: panel row `{label}` removed from "
                f"_research_modes_panel."
            )
