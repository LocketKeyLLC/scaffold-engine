"""§17.287 — unknown `/`-prefixed commands surface a "did you mean…" hint.

§17.280-UX-1 audit-tail concern: "Unknown `/`-prefixed input falls through
to triage with no feedback. The `_suggest_command()` helper is only wired
into `_handle_command()`, not the front-door dispatch. A typo like
`/resarch` silently becomes a triage turn instead of a hint."

Verification: the audit's premise was already false in production code.
`pipe()`'s front-door dispatch DOES route slash-prefixed input through
`_handle_command`, which already calls `_suggest_command(cmd)` for any
unknown command and emits a "Closest matches" reply with up to 3
candidates. The §17.280 review missed that the suggestion path runs
through `_handle_command`'s fall-through, not via the explicit dispatch
chain at lines 991-1009.

§17.287 is therefore audit-only — no production change. These tests pin
the existing contract so a future refactor that strips the suggestion
path fails loudly. Mirrors §17.282's pattern: pin an unwritten
invariant the production code already depends on.
"""
import re

import pytest

from tests._scaffold_router_setup import _mod, Pipeline


@pytest.fixture
def pipe():
    return Pipeline()


@pytest.mark.smoke
class TestSuggestCommandHelper:
    """§17.287 — the `_suggest_command(token, candidates=None)` helper
    is the building block; pin its difflib-backed contract first.
    """

    def test_close_typo_returns_known_command(self):
        """A single-edit typo should surface the original command."""
        out = _mod._suggest_command("/resarch")
        assert "/research" in out

    def test_two_char_typo_still_matches(self):
        """Slightly looser typos still cross the 0.6 cutoff."""
        out = _mod._suggest_command("/reserach")
        assert "/research" in out

    def test_unrelated_token_returns_empty(self):
        """Far-away input returns no matches — `_handle_command` then
        falls through to the generic "Unknown command" message."""
        assert _mod._suggest_command("/something_completely_unrelated") == []

    def test_returns_at_most_three(self):
        """difflib's `n=3` cap must be preserved — chat UI relies on it
        to keep the hint block scannable."""
        out = _mod._suggest_command("/res")
        assert len(out) <= 3

    def test_custom_candidate_pool_overrides_known_commands(self):
        """Subcommand handlers (`/jobs`, `/research/...`, etc.) pass
        their own candidate list. Pin that the `candidates` kwarg
        actually overrides the default `KNOWN_COMMANDS`.
        """
        out = _mod._suggest_command(
            "lst", candidates=["list", "find", "rename", "delete"]
        )
        assert out == ["list"]


@pytest.mark.smoke
class TestHandleCommandUnknownPath:
    """§17.287 — the front-door dispatch's catch-all (line 1011) routes
    unknown slash-prefixed input through `_handle_command`, which calls
    `_suggest_command` and yields the result back to chat.
    """

    def test_unknown_command_returns_hint_with_suggestions(self, pipe):
        """`/resarch` (typo of `/research`) returns an "Unknown command"
        reply that names `/research` in its close-matches list.
        Pre-§17.287 nothing pinned this — a future refactor stripping
        the suggestion call would silently regress the UX without test
        failure."""
        out = pipe._handle_command("/resarch kubernetes pods")
        assert "Unknown command: `/resarch`" in out
        assert "Closest matches:" in out
        assert "/research" in out

    def test_unknown_command_no_close_matches_falls_back_to_help(self, pipe):
        """When difflib returns no close matches, the reply is the
        generic "Type `/help` for available commands." line — no empty
        Closest-matches block."""
        out = pipe._handle_command("/zxyqwop_no_match_here")
        assert "Unknown command: `/zxyqwop_no_match_here`" in out
        assert "Closest matches:" not in out
        assert "/help" in out

    def test_suggestion_block_is_chat_friendly_markdown(self, pipe):
        """The hint should be rendered as a markdown bullet list so the
        OWUI chat renders it readable. Pin the shape so a future edit
        doesn't change it to a comma-joined string."""
        out = pipe._handle_command("/resarch x")
        # Each suggestion is prefixed by two spaces + dash + backtick.
        assert re.search(r"^\s*-\s*`/research`", out, re.MULTILINE) is not None

    def test_known_command_does_not_emit_unknown_banner(self, pipe):
        """Sanity guard: a recognized command (`/help`) doesn't get
        misrouted into the suggestion path."""
        out = pipe._handle_command("/help")
        assert "Unknown command" not in out


@pytest.mark.smoke
class TestSourceShapeRegressionGuard:
    """§17.287 — source-shape anchors. A future refactor that moves the
    front-door dispatch or strips the suggestion call should fail one
    of these instead of silently regressing the UX.
    """

    def test_front_door_routes_slash_prefix_through_handle_command(self):
        """`pipe()`'s catch-all line for slash-prefixed input must call
        `_handle_command`. If a refactor adds a triage fallback for
        unrecognized slash input, this guard catches it."""
        with open(_mod.__file__, encoding="utf-8") as f:
            src = f.read()

        # The exact pattern at line 1011-1015 area: `if msg.startswith("/")`
        # immediately followed (within a few lines) by `_handle_command(msg)`.
        # Use a regex over a short window so cosmetic whitespace tweaks
        # don't break the guard.
        m = re.search(
            r'if msg\.startswith\("/"\):.{0,200}?self\._handle_command\(msg\)',
            src, re.DOTALL,
        )
        assert m is not None, (
            "§17.287 regression: pipe()'s catch-all for unknown slash-"
            "prefixed input no longer routes through _handle_command. A "
            "typo like `/resarch` would now fall through to triage with "
            "no 'did you mean' suggestion."
        )

    def test_handle_command_calls_suggest_command_on_unknown(self):
        """`_handle_command`'s fall-through must invoke `_suggest_command`.
        Pinned so a refactor that drops the suggestion call (e.g. swaps
        it for a static "Unknown command" message) fails."""
        with open(_mod.__file__, encoding="utf-8") as f:
            src = f.read()

        assert "close = _suggest_command(cmd)" in src, (
            "§17.287 regression: _handle_command no longer calls "
            "_suggest_command(cmd) on the unknown-command path. The "
            "audit-fix shape is `close = _suggest_command(cmd); if close: "
            "...Closest matches...`. Stripping the call removes the "
            "typo-suggestion UX."
        )
        assert "Closest matches:" in src, (
            "§17.287 regression: the 'Closest matches:' chat banner text "
            "has been removed or renamed. Update the test if the wording "
            "changed intentionally."
        )
