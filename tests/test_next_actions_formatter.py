"""§17.195 — shared next_actions formatter (sdk/scaffold_client/next_actions.py).

Locks the three public helpers used by the CLI + OWUI pipeline + any
future SDK caller:

  * ``filter_renderable``  — drops noise actions (``wait``).
  * ``action_clickable``   — returns (clickable_text, description) with
                              command > endpoint > None preference.
  * ``format_block``       — full markdown/plain multi-line block.

Also asserts the byte-equal vendor invariant against ``pipelines/
_next_actions.py`` (parallel to the §17.186 schemas-in-sync and §17.190
SSE-events tests).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scaffold_client.next_actions import (
    action_clickable,
    filter_renderable,
    format_block,
)


# ---------------------------------------------------------------------------
# filter_renderable — strip noise actions
# ---------------------------------------------------------------------------

class TestFilterRenderable:
    def test_strips_wait_actions(self):
        actions = [
            {"action": "wait", "description": "queued"},
            {"action": "confirm", "command": "/confirm {job_id}", "description": "approve"},
        ]
        assert filter_renderable(actions) == [actions[1]]

    def test_keeps_all_non_wait_actions(self):
        actions = [
            {"action": "confirm", "description": "approve"},
            {"action": "retry", "description": "rerun the failed node"},
            {"action": "skip", "description": "mark skipped"},
            {"action": "delete", "description": "remove the job"},
        ]
        assert filter_renderable(actions) == actions

    def test_empty_in_empty_out(self):
        assert filter_renderable([]) == []

    def test_all_wait_returns_empty(self):
        actions = [
            {"action": "wait", "description": "queued"},
            {"action": "wait", "description": "in progress"},
        ]
        assert filter_renderable(actions) == []

    def test_does_not_mutate_input(self):
        inp = [{"action": "wait", "description": "x"},
               {"action": "confirm", "description": "y"}]
        snapshot = [dict(a) for a in inp]
        _ = filter_renderable(inp)
        assert inp == snapshot

    def test_missing_action_key_kept_as_renderable(self):
        """An action without an explicit 'action' field defaults to
        renderable — it's NOT in the noise-action set, so it survives."""
        actions = [{"description": "do the thing"}]
        assert filter_renderable(actions) == actions


# ---------------------------------------------------------------------------
# action_clickable — field selection (command > endpoint > None)
# ---------------------------------------------------------------------------

class TestActionClickable:
    def test_command_wins_over_endpoint(self):
        action = {
            "command": "/confirm abc-123",
            "endpoint": "/ideate/confirm",
            "method": "POST",
            "description": "approve",
        }
        clickable, desc = action_clickable(action)
        assert clickable == "/confirm abc-123"
        assert desc == "approve"

    def test_endpoint_used_when_no_command(self):
        action = {
            "endpoint": "/jobs/{job_id}",
            "method": "DELETE",
            "description": "drop the job",
        }
        clickable, desc = action_clickable(action)
        assert clickable == "DELETE /jobs/{job_id}"
        assert desc == "drop the job"

    def test_endpoint_defaults_to_get_method(self):
        action = {"endpoint": "/exec/status/abc", "description": "poll"}
        clickable, _ = action_clickable(action)
        assert clickable == "GET /exec/status/abc"

    def test_description_only_returns_none_clickable(self):
        action = {"description": "wait for the reaper"}
        clickable, desc = action_clickable(action)
        assert clickable is None
        assert desc == "wait for the reaper"

    def test_missing_description_falls_back_to_empty_string(self):
        action = {"command": "/foo"}
        clickable, desc = action_clickable(action)
        assert clickable == "/foo"
        assert desc == ""

    def test_empty_action_dict_returns_none_and_empty(self):
        clickable, desc = action_clickable({})
        assert clickable is None
        assert desc == ""

    def test_none_command_falls_through_to_endpoint(self):
        """A ``"command": None`` value (the registry uses this explicitly
        to mean "no command") should not be returned as clickable text."""
        action = {
            "command": None,
            "endpoint": "/exec/status/abc",
            "method": "GET",
            "description": "poll",
        }
        clickable, _ = action_clickable(action)
        assert clickable == "GET /exec/status/abc"


# ---------------------------------------------------------------------------
# format_block — full markdown/plain rendering
# ---------------------------------------------------------------------------

_SAMPLE_ACTIONS = [
    {"action": "wait", "description": "in progress"},
    {"action": "confirm", "command": "/confirm abc-123",
     "endpoint": "/ideate/confirm", "method": "POST",
     "description": "approve and proceed"},
    {"action": "delete", "command": None,
     "endpoint": "/jobs/abc-123", "method": "DELETE",
     "description": "abandon"},
    {"action": "noop", "description": "description-only entry"},
]


class TestFormatBlockMarkdown:
    def test_empty_returns_empty_string(self):
        assert format_block([]) == ""

    def test_all_wait_returns_empty_string(self):
        assert format_block(
            [{"action": "wait", "description": "x"}],
        ) == ""

    def test_markdown_header_is_bold(self):
        out = format_block(_SAMPLE_ACTIONS, style="markdown")
        assert "**Next steps:**" in out

    def test_markdown_command_wrapped_in_backticks(self):
        out = format_block(_SAMPLE_ACTIONS, style="markdown")
        assert "• `/confirm abc-123` — approve and proceed" in out

    def test_markdown_endpoint_wrapped_in_backticks(self):
        out = format_block(_SAMPLE_ACTIONS, style="markdown")
        assert "• `DELETE /jobs/abc-123` — abandon" in out

    def test_markdown_description_only_no_backticks(self):
        out = format_block(_SAMPLE_ACTIONS, style="markdown")
        assert "• description-only entry" in out
        # The description-only bullet must NOT carry backticks.
        assert "`description-only entry`" not in out


class TestFormatBlockPlain:
    def test_plain_header_no_markdown(self):
        out = format_block(_SAMPLE_ACTIONS, style="plain")
        assert "Next steps:" in out
        assert "**" not in out

    def test_plain_no_backticks_on_clickable(self):
        out = format_block(_SAMPLE_ACTIONS, style="plain")
        # The command appears unwrapped (no backticks).
        assert "/confirm abc-123" in out
        # And the line bullet uses 2-space indent.
        assert "  • /confirm abc-123" in out

    def test_plain_endpoint_method_pair_unwrapped(self):
        out = format_block(_SAMPLE_ACTIONS, style="plain")
        assert "  • DELETE /jobs/abc-123" in out

    def test_unknown_style_raises_value_error(self):
        with pytest.raises(ValueError, match="unknown style"):
            format_block(_SAMPLE_ACTIONS, style="html")

    def test_byte_identical_to_pre_17195_markdown_output(self):
        """The §17.195 refactor must NOT change the markdown bytes the
        OWUI pipeline emits — operators reading chat threads against
        pre/post-§17.195 orchestrators see the same shape."""
        expected = (
            "\n**Next steps:**\n"
            "• `/confirm abc-123` — approve and proceed\n"
            "• `DELETE /jobs/abc-123` — abandon\n"
            "• description-only entry"
        )
        assert format_block(_SAMPLE_ACTIONS, style="markdown") == expected


# ---------------------------------------------------------------------------
# Vendor byte-equal guard (parallel to §17.186 schemas-in-sync
# and §17.190 sse-events tests).
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE = REPO_ROOT / "sdk" / "scaffold_client" / "next_actions.py"
VENDORED = REPO_ROOT / "pipelines" / "_next_actions.py"


def test_next_actions_vendor_files_exist():
    assert SOURCE.is_file(), f"missing {SOURCE}"
    assert VENDORED.is_file(), f"missing {VENDORED}"


def test_next_actions_vendor_byte_equal():
    src = SOURCE.read_bytes()
    vendored = VENDORED.read_bytes()
    if src != vendored:
        raise AssertionError(
            f"{VENDORED.relative_to(REPO_ROOT)} has drifted from "
            f"{SOURCE.relative_to(REPO_ROOT)}. Run `make sync-next-actions` "
            "to refresh the vendored copy, then re-run the suite."
        )
