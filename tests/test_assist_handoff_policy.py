"""§17.621 (audit #20) — handoff_policy auto-handoff trigger in POST /assist/{sid}/submit.

The auto values (auto_on_skip / auto_all_remaining) were stored + echoed but never
consumed. On a SKIP with a non-manual policy the router now delegates to the
autonomous executor via a background handoff instead of leaving the step skipped.
These tests exercise the router-level trigger (the executor itself is covered by
the handoff_step tests + a live smoke).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.routers import assist as assist_router
from app.routers.assist import AssistSubmitInput


def _skip_body(node_key="T1"):
    return AssistSubmitInput(node_key=node_key, action="skip")


def _patch(policy):
    """Patch get_session (active + given policy), spawn, and submit_step."""
    return (
        patch.object(assist_router.assist_agent, "get_session",
                     new=AsyncMock(return_value={"status": "active", "handoff_policy": policy})),
        patch.object(assist_router.assist_agent, "spawn_handoff_background"),
        patch.object(assist_router.assist_agent, "submit_step",
                     new=AsyncMock(return_value={"status": "skipped", "committed": True})),
    )


@pytest.mark.asyncio
async def test_skip_auto_on_skip_spawns_single_handoff():
    gs, sp, ss = _patch("auto_on_skip")
    with gs, sp as spawn, ss as submit:
        result = await assist_router.assist_submit("sid-1", _skip_body(), AsyncMock())
    assert result["status"] == "auto_handoff"
    assert result["handoff_mode"] == "single"
    spawn.assert_called_once()
    assert spawn.call_args.kwargs["mode"] == "single"
    assert spawn.call_args.kwargs["node_key"] == "T1"
    submit.assert_not_called()  # the step is delegated, NOT skipped


@pytest.mark.asyncio
async def test_skip_auto_all_remaining_spawns_all_remaining():
    gs, sp, ss = _patch("auto_all_remaining")
    with gs, sp as spawn, ss as submit:
        result = await assist_router.assist_submit("sid-1", _skip_body(), AsyncMock())
    assert result["handoff_mode"] == "all_remaining"
    assert spawn.call_args.kwargs["mode"] == "all_remaining"
    submit.assert_not_called()


@pytest.mark.asyncio
async def test_skip_manual_policy_does_not_handoff():
    gs, sp, ss = _patch("manual")
    with gs, sp as spawn, ss as submit:
        result = await assist_router.assist_submit("sid-1", _skip_body(), AsyncMock())
    spawn.assert_not_called()
    submit.assert_called_once()  # normal skip path
    assert result["status"] == "skipped"


@pytest.mark.asyncio
async def test_submit_action_never_auto_handoffs():
    """A normal submit (not skip) never triggers auto-handoff, even under an auto policy."""
    with patch.object(assist_router.assist_agent, "get_session",
                      new=AsyncMock(return_value={"status": "active", "handoff_policy": "auto_on_skip"})), \
         patch.object(assist_router.assist_agent, "spawn_handoff_background") as spawn, \
         patch.object(assist_router.settings, "assist_verify_on_submit", False), \
         patch.object(assist_router.assist_agent, "submit_step",
                      new=AsyncMock(return_value={"status": "committed"})) as submit:
        result = await assist_router.assist_submit(
            "sid-1", AssistSubmitInput(node_key="T1", action="submit", output="did it"), AsyncMock(),
        )
    spawn.assert_not_called()
    submit.assert_called_once()
    assert result["status"] == "committed"
