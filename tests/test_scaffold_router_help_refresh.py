"""§17.306 — `/help` refreshed for first-touch operators.

Pre-§17.306 `/help` was a 6-section table of 22+ commands prefaced by
a single workflow sentence. Operators arriving from §17.300's welcome
(which points at `/help` as the discovery exit) hit a wall of text —
no concrete starter examples and no canonical scenario walkthroughs.

§17.306 keeps every command listed but adds:
  - A "Try one of these to start" block at the TOP with 3 concrete,
    copy-pasteable starter commands (mirrors the §17.300 welcome to
    reinforce the first-touch surface).
  - A "Common scenarios" footer with 4 walkthroughs:
    chat-then-/go, one-shot /idea, failed-node recovery, mid-flight
    cost inspection.
  - Tightened command descriptions (column header renamed
    "Description" → "What it does"; verbiage trimmed).

The 22-command surface is preserved — the existing
TestHelp.test_contains_key_commands in test_scaffold_router_helpers.py
continues to load-bear that contract.

These tests pin the §17.306 additions specifically:
  - Starter block present + has all 3 commands from the welcome
  - Common scenarios section present + names all 4 scenarios
  - Canonical flow line present + uses the post-§17.306 phrasing
  - Source-shape guards anchor each new section
"""
import re

import pytest

from tests._scaffold_router_setup import Pipeline


@pytest.fixture
def pipe():
    return Pipeline()


@pytest.mark.smoke
class TestStarterBlock:
    """§17.306 — the "Try one of these to start" block at the top."""

    def test_starter_section_present(self, pipe):
        out = pipe._help()
        assert "Try one of these to start:" in out, (
            "§17.306: /help must surface a copy-pasteable starter block "
            "above the command tables. Operators arriving from the "
            "welcome shouldn't have to scan 22 commands to find one to try."
        )

    def test_starter_examples_match_welcome(self, pipe):
        """The 3 starter examples must mirror §17.300's welcome, so an
        operator who saw welcome → typed /help sees CONSISTENT exits."""
        out = pipe._help()
        # Same shape as the welcome's bullet list — concrete commands
        # the operator can copy-paste verbatim.
        assert "/idea Build a CLI that converts screenshots to PDF" in out, (
            "§17.306 + §17.300 contract: the /idea starter example "
            "must match the welcome's exemplar"
        )
        assert "/research kubernetes best practices" in out
        assert "`/jobs`" in out

    def test_starter_block_before_first_table(self, pipe):
        """Position matters — the starter block above the first table
        means scrollers see it without scanning. Below the tables it
        gets buried."""
        out = pipe._help()
        starter_idx = out.index("Try one of these to start:")
        # First section header — the dispatch table for `Scope & kickoff`.
        first_table_idx = out.index("Scope & kickoff")
        assert starter_idx < first_table_idx, (
            "§17.306: starter block must appear ABOVE the first command "
            "table, not below it. Operators scrolling the wall of "
            "tables miss it otherwise."
        )


@pytest.mark.smoke
class TestCommonScenariosSection:
    """§17.306 — the "Common scenarios" footer."""

    def test_section_present(self, pipe):
        out = pipe._help()
        assert "Common scenarios" in out, (
            "§17.306: /help must include a `Common scenarios` section "
            "that walks the operator through canonical flows."
        )

    def test_all_four_scenarios_named(self, pipe):
        out = pipe._help()
        # Each scenario starts with a bolded title at the head of a bullet.
        for label in (
            "Launch from a conversation",
            "One-shot launch",
            "Recover from a failed node",
            "Inspect cost mid-flight",
        ):
            assert label in out, (
                f"§17.306: Common scenarios must include the "
                f"`{label}` walkthrough."
            )

    def test_scenarios_reference_real_commands(self, pipe):
        """Each scenario must name the actual command(s) the operator
        would type — not abstract verbs. Catches a drift toward
        generic prose."""
        out = pipe._help()
        scenarios_block = out.split("Common scenarios", 1)[1]
        # Each scenario must reference at least one real command.
        for cmd in ("/go", "/idea", "/exec retry", "/cost"):
            assert cmd in scenarios_block, (
                f"§17.306: Common scenarios section must mention `{cmd}` "
                f"— the canonical flows lose their copy-paste value "
                f"without the actual command names."
            )

    def test_recovery_scenario_chains_results_then_retry(self, pipe):
        """The failed-node recovery flow is the highest-stakes scenario.
        It must show /results FIRST (diagnose) then /exec retry (act).
        Reversed order would tell operators to retry before
        understanding what broke."""
        out = pipe._help()
        scenarios_block = out.split("Common scenarios", 1)[1]
        recovery = scenarios_block.split("Recover from a failed node", 1)[1]
        # /results must appear before /exec retry in this scenario.
        results_idx = recovery.index("/results")
        retry_idx = recovery.index("/exec retry")
        assert results_idx < retry_idx, (
            "§17.306: the failed-node recovery scenario must teach "
            "diagnose-before-act ordering — /results before /exec retry."
        )


@pytest.mark.smoke
class TestCanonicalFlowLine:
    """§17.306 — pin the post-refresh phrasing of the canonical flow."""

    def test_canonical_flow_line_present(self, pipe):
        out = pipe._help()
        assert "Canonical flow:" in out, (
            "§17.306: the canonical flow sentence is the load-bearing "
            "summary above all command tables. Removing it strands "
            "operators in the table-soup."
        )

    def test_canonical_flow_names_all_stages(self, pipe):
        """The flow names every stage in the operator journey:
        chat → /go → review → /confirm → execute."""
        out = pipe._help()
        flow_line = out.split("Canonical flow:", 1)[1].split("\n", 1)[0]
        for stage in ("chat", "/go", "review", "/confirm", "execute"):
            assert stage in flow_line, (
                f"§17.306: canonical flow line missing the `{stage}` "
                f"stage. The full chat → go → review → confirm → execute "
                f"journey must be visible in one sentence."
            )


@pytest.mark.smoke
class TestColumnHeaderRefresh:
    """§17.306 — table column header changed `Description` → `What it does`."""

    def test_new_column_header_present(self, pipe):
        out = pipe._help()
        # At least one section should have the new header.
        assert "| What it does |" in out, (
            "§17.306: the column header `What it does` is the post-refresh "
            "shape. Reverting to `Description` regresses the active-voice "
            "framing introduced in §17.306."
        )

    def test_all_sections_use_new_header(self, pipe):
        """The 6 command tables all share the same header — drift here
        would mean a partial refresh."""
        out = pipe._help()
        # Count `What it does` occurrences — one per table header.
        count = out.count("| What it does |")
        assert count >= 6, (
            f"§17.306: only {count} tables use `What it does` header. "
            f"Expected ≥ 6 (one per section). Partial refresh = drift."
        )


@pytest.mark.smoke
class TestPreservedSurface:
    """§17.306 — pin that the refresh PRESERVES every command + the
    workflow guidance. Belt-and-suspenders next to
    TestHelp.test_contains_key_commands in test_scaffold_router_helpers.py
    which loads the 9-command floor."""

    def test_all_22_commands_still_listed(self, pipe):
        """Every command surfaced pre-§17.306 must still appear. Catches
        a "tighten descriptions" pass that drops a command row by
        accident."""
        out = pipe._help()
        commands = [
            "/go", "/run", "/idea", "/confirm",
            "/execute", "/skip", "/results", "/status",
            "/rag", "/research", "/research/reply", "/research/pdf",
            "/jobs", "/research/", "/schedule",
            "/model", "/optimize", "/config", "/help",
            "/health", "/logs", "/exec retry", "/cleanup", "/cost",
        ]
        for cmd in commands:
            assert cmd in out, (
                f"§17.306 regression: `{cmd}` is no longer listed in "
                f"/help. The refresh must preserve the full surface."
            )

    def test_native_web_ui_footer_anchored(self, pipe):
        """The footer that points operators at the native web UI is the
        non-chat discovery exit — must survive the refresh."""
        out = pipe._help()
        assert "web/jobs" in out, (
            "§17.306: the native web UI footer reference is gone. "
            "Operators lose the non-chat discovery surface."
        )

    def test_full_reference_pointer_present(self, pipe):
        out = pipe._help()
        assert "USER_GUIDE.md" in out
        assert "README.md" in out


@pytest.mark.smoke
class TestSourceShapeRegressionGuard:

    def _src(self) -> str:
        from pipelines import scaffold_router
        with open(scaffold_router.__file__, encoding="utf-8") as f:
            return f.read()

    def test_starter_block_anchored_in_source(self):
        src = self._src()
        assert "Try one of these to start:" in src, (
            "§17.306 regression: starter block phrasing is gone from "
            "scaffold_router.py's _help."
        )

    def test_common_scenarios_anchored_in_source(self):
        src = self._src()
        assert "Common scenarios" in src, (
            "§17.306 regression: Common scenarios section is gone."
        )

    def test_canonical_flow_anchored_in_source(self):
        src = self._src()
        assert "Canonical flow:" in src, (
            "§17.306 regression: canonical flow phrasing has been "
            "replaced or removed."
        )

    def test_all_four_scenario_labels_anchored(self):
        """Anchor each scenario label individually — a refactor that
        consolidates them into fewer scenarios should trip here so
        the loss of coverage is visible at review."""
        src = self._src()
        for label in (
            "Launch from a conversation",
            "One-shot launch",
            "Recover from a failed node",
            "Inspect cost mid-flight",
        ):
            assert label in src, (
                f"§17.306 regression: scenario label `{label}` is "
                f"missing from _help. Walkthrough coverage regressed."
            )
