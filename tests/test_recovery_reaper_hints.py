"""Tests for §17.134 — reaper-driven next_actions hints.

Covers:
  - classify_error_summary recognizes every cleanup.py pattern.
  - Unknown / empty / None summaries classify to None.
  - next_actions_for prepends reason-specific actions and annotates
    them with `reason_kind`.
  - Each reason path matches the appropriate status.
  - Backward compat: omitting error_summary preserves the prior
    behavior byte-for-byte.
"""
from __future__ import annotations

import pytest

from app.modules.recovery import (
    NEXT_ACTIONS,
    REAPER_REASON_ACTIONS,
    _REAPER_REASON_PATTERNS,
    classify_error_summary,
    next_actions_for,
)


# ---------------------------------------------------------------------------
# classify_error_summary
# ---------------------------------------------------------------------------

class TestClassify:
    def test_none_returns_none(self):
        assert classify_error_summary(None) is None

    def test_empty_returns_none(self):
        assert classify_error_summary("") is None

    def test_unknown_returns_none(self):
        assert classify_error_summary("some user-supplied failure detail") is None

    @pytest.mark.parametrize("summary,expected_kind", [
        # The exact strings emitted by app/modules/cleanup.py — must match.
        ("Awaiting confirmation gate timeout (no user reply)",
         "reaper_awaiting_confirmation"),
        ("Stale planning state — exceeded planning_stale_minutes",
         "reaper_planning_stale"),
        ("Assist session abandoned (idle > threshold)",
         "reaper_assist_abandoned"),
        ("Long-phase job timed out after 45 minutes of inactivity",
         "reaper_long_phase_timeout"),
        ("Research session timed out after 30 minutes of inactivity",
         "reaper_research_session_timeout"),
        ("Pause expired before user reply received",
         "reaper_paused_research_expired"),
        ("Job timed out after 30 minutes of inactivity",
         "reaper_execution_timeout"),
        ("client_disconnect",
         "phase2_client_disconnect"),
    ])
    def test_known_summaries_classify(self, summary, expected_kind):
        assert classify_error_summary(summary) == expected_kind

    def test_long_phase_pattern_disambiguates_from_execution(self):
        """'Long-phase' must match BEFORE 'Job timed out after' — both
        contain 'timed out', but the long-phase string is more specific."""
        long_phase = "Long-phase job timed out after 45 minutes of inactivity"
        assert classify_error_summary(long_phase) == "reaper_long_phase_timeout"

    def test_every_pattern_has_a_reason_actions_entry(self):
        """Every classified reason_kind must have an actions template."""
        for _, reason_kind in _REAPER_REASON_PATTERNS:
            assert reason_kind in REAPER_REASON_ACTIONS, (
                f"missing REAPER_REASON_ACTIONS entry for {reason_kind}"
            )


# ---------------------------------------------------------------------------
# next_actions_for — prepend behavior
# ---------------------------------------------------------------------------

class TestPrepending:
    def test_no_error_summary_preserves_base_behavior(self):
        """Backward compat: omitting error_summary returns the unchanged
        base actions for the status."""
        without = next_actions_for("cancelled", "job-1")
        base_count = len(NEXT_ACTIONS["cancelled"])
        assert len(without) == base_count
        # No reason_kind annotation on any action when no summary
        assert all("reason_kind" not in a for a in without)

    def test_unknown_summary_does_not_prepend(self):
        """An unrecognized error_summary falls through to the base."""
        out = next_actions_for("cancelled", "job-1", error_summary="weird user thing")
        assert len(out) == len(NEXT_ACTIONS["cancelled"])
        assert all("reason_kind" not in a for a in out)

    def test_awaiting_confirmation_reap_prepends_resume(self):
        """Reaped awaiting_confirmation → cancelled. The resume action
        gets prepended before the generic rerun/delete pair."""
        out = next_actions_for(
            "cancelled", "job-uuid-123",
            error_summary="Awaiting confirmation gate timeout (no user reply)",
        )
        assert len(out) == len(NEXT_ACTIONS["cancelled"]) + 1
        first = out[0]
        assert first["action"] == "resume"
        assert first["endpoint"] == "/jobs/job-uuid-123/resume"
        assert first["method"] == "POST"
        assert first["reason_kind"] == "reaper_awaiting_confirmation"

    def test_planning_stale_reap_prepends_resume(self):
        out = next_actions_for(
            "cancelled", "job-9",
            error_summary="Stale planning state — exceeded planning_stale_minutes",
        )
        first = out[0]
        assert first["action"] == "resume"
        assert first["endpoint"] == "/jobs/job-9/resume"
        assert first["reason_kind"] == "reaper_planning_stale"

    def test_assist_abandoned_reap_prepends_restart(self):
        out = next_actions_for(
            "cancelled", "job-7",
            error_summary="Assist session abandoned (idle > threshold)",
        )
        first = out[0]
        assert first["action"] == "restart_assist"
        assert first["reason_kind"] == "reaper_assist_abandoned"

    def test_execution_timeout_reap_prepends_retry_node(self):
        """Reaped from running/executing → failed. Retry the stuck node."""
        out = next_actions_for(
            "failed", "job-5",
            failed_node_key="T3",
            error_summary="Job timed out after 30 minutes of inactivity",
        )
        first = out[0]
        assert first["action"] == "retry_node"
        assert first["command"] == "/exec retry job-5 T3"
        assert first["endpoint"] == "/exec/retry"
        assert first["reason_kind"] == "reaper_execution_timeout"

    def test_long_phase_reap_prepends_rerun(self):
        out = next_actions_for(
            "failed", "job-2",
            error_summary="Long-phase job timed out after 45 minutes of inactivity",
        )
        first = out[0]
        assert first["action"] == "rerun"
        assert first["endpoint"] == "/ideate"
        assert first["reason_kind"] == "reaper_long_phase_timeout"

    def test_client_disconnect_prepends_reconfirm(self):
        out = next_actions_for(
            "failed", "job-4",
            error_summary="client_disconnect",
        )
        first = out[0]
        assert first["action"] == "reconfirm"
        assert first["command"] == "/confirm job-4"
        assert first["reason_kind"] == "phase2_client_disconnect"

    def test_base_actions_still_appended_after_prepend(self):
        """The reason-specific action does NOT replace the base — base
        actions still follow, so generic remediation remains discoverable."""
        out = next_actions_for(
            "cancelled", "job-1",
            error_summary="Awaiting confirmation gate timeout (no user reply)",
        )
        base_actions = [a["action"] for a in NEXT_ACTIONS["cancelled"]]
        rendered_actions = [a["action"] for a in out]
        # First is the prepended resume; the rest are the base.
        assert rendered_actions[0] == "resume"
        for base_action in base_actions:
            assert base_action in rendered_actions[1:]

    def test_reason_kind_only_on_prepended_entries(self):
        """Base actions must NOT pick up reason_kind — it's a flag for
        the prepended diagnostic actions specifically."""
        out = next_actions_for(
            "cancelled", "job-1",
            error_summary="Awaiting confirmation gate timeout (no user reply)",
        )
        # First action has reason_kind
        assert out[0]["reason_kind"] == "reaper_awaiting_confirmation"
        # Base actions don't
        for action in out[1:]:
            assert "reason_kind" not in action


# ---------------------------------------------------------------------------
# Backward compat — existing tests must keep passing
# ---------------------------------------------------------------------------

def test_unknown_status_still_returns_empty(caplog):
    """Backward compat: unknown status returns []. error_summary doesn't
    change that — it would be confusing to invent actions for an unknown
    state."""
    with caplog.at_level("WARNING", logger="scaffold.recovery"):
        out = next_actions_for(
            "made_up_status", "job-1",
            error_summary="Awaiting confirmation gate timeout (no user reply)",
        )
    assert out == []
    assert any("recovery_unknown_status" in r.getMessage() for r in caplog.records)
