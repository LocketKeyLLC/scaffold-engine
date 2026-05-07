"""Tests for app/modules/recovery.py — the per-job-status next-action
registry that turns the existing `jobs.status` lifecycle into structured
guidance for the OWUI pipeline, CLI, and SDK.
"""
from __future__ import annotations

import pytest

from app.modules.recovery import (
    NEXT_ACTIONS,
    all_known_statuses,
    next_actions_for,
)
from app.schemas import JOB_STATUSES


def test_registry_covers_every_known_job_status():
    """If a new status is added to JobStatus, the registry must learn
    about it explicitly — silent omissions would degrade UX."""
    missing = set(JOB_STATUSES) - set(all_known_statuses())
    assert not missing, f"recovery.NEXT_ACTIONS missing entries for: {sorted(missing)}"


def test_registry_only_uses_known_statuses():
    """Reverse direction: registry must not reference statuses no longer
    in JobStatus (would render guidance for impossible states)."""
    extra = set(all_known_statuses()) - set(JOB_STATUSES)
    assert not extra, f"recovery.NEXT_ACTIONS references unknown statuses: {sorted(extra)}"


def test_every_action_has_required_keys():
    required = {"action", "command", "endpoint", "method", "description", "node_specific"}
    for status, actions in NEXT_ACTIONS.items():
        for action in actions:
            assert set(action.keys()) >= required, (
                f"status={status} action={action.get('action')} missing keys: "
                f"{required - set(action.keys())}"
            )


def test_resolves_placeholders_with_concrete_job_id():
    actions = next_actions_for("awaiting_confirmation", "abc-123")
    confirm = next(a for a in actions if a["action"] == "confirm")
    assert confirm["command"] == "/confirm abc-123"
    assert confirm["endpoint"] == "/ideate/confirm"
    delete = next(a for a in actions if a["action"] == "delete")
    assert delete["endpoint"] == "/jobs/abc-123"


def test_resolves_failed_node_key_into_retry_command():
    actions = next_actions_for("failed", "abc-123", failed_node_key="T2")
    retry = next(a for a in actions if a["action"] == "retry_node")
    assert retry["command"] == "/exec retry abc-123 T2"
    skip = next(a for a in actions if a["action"] == "skip_node")
    assert skip["command"] == "/skip abc-123 T2"


def test_node_specific_actions_keep_placeholder_when_no_context():
    """Caller may not yet know which node failed (e.g. listing screen);
    keep `{node_key}` literal so the caller can render verbatim or
    request the user supply it."""
    actions = next_actions_for("failed", "abc-123")
    retry = next(a for a in actions if a["action"] == "retry_node")
    assert retry["command"] == "/exec retry abc-123 {node_key}"


def test_unknown_status_returns_empty_list():
    actions = next_actions_for("totally_made_up_status", "abc-123")
    assert actions == []


def test_completed_returns_view_output_action():
    actions = next_actions_for("completed", "abc-123")
    assert len(actions) == 1
    assert actions[0]["action"] == "view_output"
    assert actions[0]["command"] == "/results abc-123"


def test_blocked_offers_retry_and_skip():
    actions = next_actions_for("blocked", "abc-123", blocked_node_key="T5")
    action_kinds = [a["action"] for a in actions]
    assert "retry_node" in action_kinds
    assert "skip_node" in action_kinds
    retry = next(a for a in actions if a["action"] == "retry_node")
    assert "T5" in retry["command"]


def test_assist_statuses_emit_assist_commands():
    """Assist Mode statuses get assist-flavored next actions, not the
    plain DAG-execution ones."""
    actions = next_actions_for("assisted_executing", "session-123")
    assert any(a["command"] and "/assist next" in a["command"] for a in actions)
    actions = next_actions_for("assisted_paused", "session-123")
    assert any(a["command"] and "/assist resume" in a["command"] for a in actions)


def test_resolved_action_does_not_mutate_registry():
    """Ensure next_actions_for returns deep-enough copies that callers
    can't accidentally edit the global registry."""
    pre_len = len(NEXT_ACTIONS["failed"])
    actions = next_actions_for("failed", "abc-123", failed_node_key="T1")
    actions[0]["command"] = "MUTATED"
    actions[0]["new_key"] = "garbage"
    # Re-resolve and confirm the registry wasn't affected.
    fresh = next_actions_for("failed", "abc-123", failed_node_key="T1")
    assert fresh[0]["command"] == "/exec retry abc-123 T1"
    assert "new_key" not in fresh[0]
    assert len(NEXT_ACTIONS["failed"]) == pre_len


@pytest.mark.parametrize("status", JOB_STATUSES)
def test_every_status_returns_at_least_one_action(status):
    """No status should resolve to an empty action list — even
    `cancelled` should at least suggest delete."""
    actions = next_actions_for(status, "abc-123")
    assert len(actions) >= 1, f"status={status} has no actions"
