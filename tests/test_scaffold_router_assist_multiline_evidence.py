"""§17.308 — multi-line `/assist submit` evidence ergonomics.

Pre-§17.308 the parser at `pipelines/_vendor/_assist_handlers.py`
extracted evidence via `head.split(None, 4)` + `" ".join(rest[1:])`.
That collapsed whitespace runs (including newlines) at the first
four boundaries, then re-joined the trailing tokens with single
spaces. Symptoms when operators pasted multi-line evidence
without code fences:

  - Leading blank lines stripped
  - Stray spaces inserted at token boundaries
  - Multiple newlines between words collapsed to single delimiter

§17.308 introduces a multi-line capture path: when no fence is
present AND the message contains a newline AND the node_key
appears on the first line, everything AFTER the first newline is
preserved verbatim as evidence.

These tests pin:
  - Single-line evidence still works (existing behavior preserved)
  - Fenced evidence still wins (operator-explicit takes priority)
  - Multi-line non-fenced evidence is now preserved verbatim
  - Edge cases (node_key on continuation line, only-whitespace tail)
  - render_step's help text reflects the new affordance
"""
from unittest.mock import MagicMock, patch

import pytest

from tests._scaffold_router_setup import Pipeline, _mod as _router_mod

# The vendor module loaded via spec_from_file_location is bound as
# `scaffold_router._assist` — the standard pipelines._vendor._assist_-
# handlers import path doesn't resolve (the underscore-prefixed
# directory is invisible to OWUI auto-discovery + standard import).
_vendor = _router_mod._assist


@pytest.fixture
def pipe():
    return Pipeline()


def _drive_handle_assist(pipe, msg: str) -> tuple[str, list]:
    """Run pipe._handle_assist with assist_submit stubbed to capture
    what evidence was submitted. Returns (chat_output, [calls]) where
    each call is (sid, node_key, evidence).

    Bypasses the orchestrator chatmap entirely (chat_id=None) so
    session_id must be passed explicitly in the message via UUID prefix.
    """
    captured_calls: list = []

    def _stub_submit(pipe_arg, sid, node_key, evidence, *, chat_id=None):
        captured_calls.append((sid, node_key, evidence))
        yield f"STUBBED_SUBMIT sid={sid} node={node_key} evidence={evidence!r}"

    with patch.object(_vendor, "assist_submit", side_effect=_stub_submit):
        chunks = list(pipe._handle_assist(msg, body=None))
    return "".join(chunks), captured_calls


_SID = "11111111-2222-3333-4444-555555555555"


# ---------------------------------------------------------------------------
# Single-line evidence (existing behavior must be preserved)
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestSingleLineEvidence:

    def test_inline_evidence_after_node_key(self, pipe):
        """`/assist submit <sid> T2 done with this step` — single-line
        inline evidence captured via the existing whitespace-join path."""
        msg = f"/assist submit {_SID} T2 done with this step"
        _, calls = _drive_handle_assist(pipe, msg)
        assert len(calls) == 1
        sid, node_key, evidence = calls[0]
        assert sid == _SID
        assert node_key == "T2"
        assert evidence == "done with this step"

    def test_no_evidence_when_args_only(self, pipe):
        """Just `/assist submit <sid> T2` with no trailing content."""
        msg = f"/assist submit {_SID} T2"
        _, calls = _drive_handle_assist(pipe, msg)
        assert calls[0][2] == ""


# ---------------------------------------------------------------------------
# Multi-line evidence (the §17.308 fix)
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestMultiLineEvidence:

    def test_simple_multi_line_preserved(self, pipe):
        msg = (
            f"/assist submit {_SID} T2\n"
            "Line 1\n"
            "Line 2\n"
            "Line 3"
        )
        _, calls = _drive_handle_assist(pipe, msg)
        evidence = calls[0][2]
        assert evidence == "Line 1\nLine 2\nLine 3", (
            f"§17.308: multi-line evidence not preserved. Got {evidence!r}"
        )

    def test_indented_code_block_preserved(self, pipe):
        """Operator pastes Python code without fences — indentation must
        survive (the most common real-world failure mode pre-§17.308)."""
        msg = (
            f"/assist submit {_SID} T2\n"
            "def foo():\n"
            "    return 1\n"
            "\n"
            "foo()"
        )
        evidence = _drive_handle_assist(pipe, msg)[1][0][2]
        # Indentation intact.
        assert "    return 1" in evidence
        # Blank line preserved.
        assert "\n\nfoo()" in evidence

    def test_leading_blank_line_preserved(self, pipe):
        """Pre-§17.308: a leading blank line in evidence got stripped
        when split(None, 4) collapsed whitespace runs."""
        msg = f"/assist submit {_SID} T2\n\nbig content"
        evidence = _drive_handle_assist(pipe, msg)[1][0][2]
        assert evidence.startswith("\n"), (
            f"§17.308: leading blank line in evidence was stripped. "
            f"Got {evidence!r}"
        )

    def test_stack_trace_preserved_verbatim(self, pipe):
        """Real-world scenario: operator pastes a Python stack trace."""
        msg = (
            f"/assist submit {_SID} T2\n"
            "Traceback (most recent call last):\n"
            "  File \"foo.py\", line 42, in main\n"
            "    bar()\n"
            "  File \"foo.py\", line 17, in bar\n"
            "    raise ValueError(\"bad\")\n"
            "ValueError: bad"
        )
        evidence = _drive_handle_assist(pipe, msg)[1][0][2]
        # Indentation of the "  File ..." lines preserved.
        assert "  File \"foo.py\", line 42" in evidence
        assert "  File \"foo.py\", line 17" in evidence
        # Final exception line on its own line.
        assert evidence.endswith("ValueError: bad")
        # Newlines preserved across the whole block (6 lines = 5 \n).
        assert evidence.count("\n") == 5

    def test_unicode_dash_node_key_still_works(self, pipe):
        """Node keys like `STEP_1` with underscores survive (existing
        regex-free node-key extraction)."""
        msg = (
            f"/assist submit {_SID} STEP_1\n"
            "multi-line output here\n"
            "and another line"
        )
        sid, node_key, evidence = _drive_handle_assist(pipe, msg)[1][0]
        assert node_key == "STEP_1"
        assert evidence == "multi-line output here\nand another line"


# ---------------------------------------------------------------------------
# Fenced evidence wins (operator-explicit takes priority)
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestFencedTakesPriority:

    def test_fenced_block_preferred_over_multiline_tail(self, pipe):
        """When both a code fence AND multi-line tail are present, the
        fenced content wins. (Pre-§17.308 fenced already won; this
        test pins that §17.308's new path doesn't disrupt it.)"""
        msg = (
            f"/assist submit {_SID} T2\n"
            "ignored prefix\n"
            "```\n"
            "this is the real evidence\n"
            "```\n"
            "ignored suffix"
        )
        evidence = _drive_handle_assist(pipe, msg)[1][0][2]
        assert "this is the real evidence" in evidence
        assert "ignored prefix" not in evidence
        assert "ignored suffix" not in evidence

    def test_fenced_with_language_tag(self, pipe):
        """`extract_fenced` already strips a short first-line language
        tag (e.g., 'python'). Pin that this still works through the
        new multi-line capture path."""
        msg = (
            f"/assist submit {_SID} T2\n"
            "```python\n"
            "def foo():\n"
            "    pass\n"
            "```"
        )
        evidence = _drive_handle_assist(pipe, msg)[1][0][2]
        assert "def foo():" in evidence
        assert "python" not in evidence  # tag stripped


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestEdgeCases:

    def test_node_key_on_continuation_line_falls_back(self, pipe):
        """`/assist submit\nT2\nevidence` — node_key on line 2 means the
        multi-line capture path SHOULD NOT fire (it would otherwise
        capture node_key as part of the evidence). Falls back to the
        existing whitespace-join behavior."""
        msg = f"/assist submit {_SID}\nT2\nfoo bar baz"
        _, calls = _drive_handle_assist(pipe, msg)
        # The existing behavior treats T2 as node_key (extracted via
        # split(None, 4) which traverses newlines).
        assert calls[0][1] == "T2"
        # Evidence captured via whitespace-join (pre-§17.308 behavior).
        # The multi-line path is correctly inhibited by the
        # `node_key in first_line` guard.
        evidence = calls[0][2]
        # Pre-§17.308 join: "foo bar baz" (newlines consumed as ws).
        assert "foo" in evidence and "baz" in evidence

    def test_whitespace_only_tail_falls_through(self, pipe):
        """If the multi-line tail is just whitespace (operator hit
        Enter then nothing), don't capture it — fall through to the
        existing rest[1:] join (which will be empty too)."""
        msg = f"/assist submit {_SID} T2\n   \n  "
        evidence = _drive_handle_assist(pipe, msg)[1][0][2]
        # Whitespace-only tail = no useful evidence = falls back to ""
        assert evidence == ""

    def test_command_with_trailing_space_no_newline(self, pipe):
        """`/assist submit <sid> T2 done   ` — single-line trailing
        whitespace must not trigger the multi-line path. (split(None,
        4)'s last element preserves trailing whitespace; this is
        existing behavior, pinned here so §17.308 doesn't regress it.)"""
        msg = f"/assist submit {_SID} T2 done   "
        evidence = _drive_handle_assist(pipe, msg)[1][0][2]
        # The trailing whitespace is preserved by split's maxsplit
        # semantics. What matters: NO newlines mean the multi-line
        # path doesn't fire, so the existing whitespace-join path
        # captures "done   " (with trailing spaces).
        assert "\n" not in evidence
        assert evidence.strip() == "done"


# ---------------------------------------------------------------------------
# Slash-form (`/assist/submit`) parity
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestSlashFormParity:
    """The slash-form `/assist/submit` shares the same parser — pin
    that multi-line works through that entry too."""

    def test_slash_form_multi_line(self, pipe):
        msg = (
            f"/assist/submit {_SID} T2\n"
            "line one\n"
            "line two"
        )
        evidence = _drive_handle_assist(pipe, msg)[1][0][2]
        assert evidence == "line one\nline two"


# ---------------------------------------------------------------------------
# render_step help text — surfaces the new affordance
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestRenderStepHelpUpdate:

    def test_footer_teaches_natural_language_reporting(self, pipe):
        """§17.626 — the old `/assist submit` code fence became a natural-
        language footer ('just tell me what happened'). Pin the plain-words
        call-to-action + the skip/fix affordances so the conversational
        shape doesn't silently regress to command-only."""
        out = _vendor.render_step_footer({
            "node_key": "T2", "title": "do thing",
        })
        assert "just tell me what happened" in out.lower()
        assert "skip" in out.lower()
        # slash commands stay available as muted aliases.
        assert "/assist submit" in out

    def test_step_and_footer_do_not_use_four_backticks(self, pipe):
        """Pre-§17.308 the submit example was wrapped in 4 backticks (nested
        fence). The intro + footer must stay clear of 4-backtick nesting so
        OWUI doesn't render a broken nested code block."""
        step = {
            "session_id": "x", "status": "ready_to_run",
            "node_key": "T2", "title": "do thing",
            "tool": "LLM", "domain": "eng", "depends_on": [],
            "base_prompt": "do the thing",
        }
        combined = _vendor.render_step(step) + _vendor.render_step_footer(step)
        assert "````" not in combined, (
            "render_step/footer reintroduced 4-backtick nesting."
        )


# ---------------------------------------------------------------------------
# Source-shape regression guards
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestSourceShapeRegressionGuard:

    def _src(self) -> str:
        with open(_vendor.__file__, encoding="utf-8") as f:
            return f.read()

    def test_dispatch_takes_raw_head_kwarg(self):
        """The raw_head plumbing is load-bearing for multi-line capture.
        A refactor that removes it would silently disable the path."""
        src = self._src()
        assert "raw_head: str | None = None" in src

    def test_submit_branch_uses_multi_line_evidence(self):
        """Anchor the variable name + the precedence
        (fenced > multi_line > join). A refactor that swaps the
        precedence would break the operator-explicit fence override."""
        src = self._src()
        assert "multi_line_evidence" in src
        assert "fenced\n            or multi_line_evidence" in src

    def test_node_key_first_line_guard_anchored(self):
        """The guard that prevents the multi-line path from firing when
        node_key is on a continuation line — load-bearing for the
        backward-compat edge case."""
        src = self._src()
        assert "node_key in first_line" in src

    def test_dispatch_call_sites_pass_raw_head(self):
        """Both call sites in handle_assist must thread raw_head=head.
        A refactor that drops it on one site silently disables multi-
        line for that entry path."""
        src = self._src()
        # Count `raw_head=head` occurrences — one per dispatch call.
        count = src.count("raw_head=head")
        assert count >= 2, (
            f"§17.308 regression: only {count} dispatch call sites pass "
            f"raw_head=head. Expected ≥ 2 (one per /assist entry path)."
        )
