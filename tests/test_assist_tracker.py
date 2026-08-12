"""§17.754 — the progress-tracking agent: reconcile the session pointer with where
the operator actually is, and drive add_step when they've moved to an uncovered
sub-task. Fail-soft to on_step so a flaky agent never traps the turn.
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.modules import assist_agent, assist_tracker
from app.providers.base import ToolCall


def _result(first_=None, all_=None):
    m = MagicMock()
    m.mappings.return_value.first.return_value = first_
    m.mappings.return_value.all.return_value = all_ if all_ is not None else []
    return m


def _sess_and_nodes():
    return AsyncMock(side_effect=[
        _result(first_={"job_id": "j1", "status": "active",
                        "current_node_key": "T13", "metadata": {}}),
        _result(all_=[
            {"node_key": "T13", "title": "Install guest OS", "status": "pending"},
            {"node_key": "T14", "title": "Install NVIDIA driver", "status": "pending"},
        ]),
    ])


@pytest.mark.asyncio
async def test_tracker_disabled_is_noop(monkeypatch):
    monkeypatch.setattr(settings, "assist_progress_tracker_enabled", False, raising=False)
    out = await assist_tracker.assess_progress(
        session_id="s", message="help me set up the network", db=AsyncMock())
    assert out["verdict"] == "on_step" and out["reason"] == "tracker_disabled"


@pytest.mark.asyncio
async def test_tracker_detects_uncovered_subtask(monkeypatch):
    monkeypatch.setattr(settings, "assist_progress_tracker_enabled", True, raising=False)
    db = AsyncMock(); db.execute = _sess_and_nodes()
    resp = types.SimpleNamespace(
        text="", success=True, error=None,
        tool_calls=[ToolCall(id="t", name="report_progress", arguments={
            "verdict": "add_step", "current_step_done": True, "covered_by_node": "",
            "new_step_title": "Configure guest network on the installed Ubuntu server",
            "new_step_request": "add a step to configure networking on the freshly "
                                "installed Ubuntu guest so it has a working IP and internet",
            "confidence": 0.9, "reason": "install done; network config not covered"})])
    with patch.object(assist_agent, "get_step_recap", new=AsyncMock(return_value="")), \
         patch.object(assist_agent, "_history_or_transcript", new=AsyncMock(return_value=[])), \
         patch("app.model_router.tool_call", new=AsyncMock(return_value=resp)):
        out = await assist_tracker.assess_progress(
            session_id="s",
            message="i need help setting up the network on the installed ubuntu server",
            db=db)
    assert out["verdict"] == "add_step"
    assert out["current_step_done"] is True
    assert out["confidence"] == 0.9
    assert "network" in out["new_step_request"].lower()
    assert out["new_step_title"]


@pytest.mark.asyncio
async def test_tracker_on_step_default(monkeypatch):
    monkeypatch.setattr(settings, "assist_progress_tracker_enabled", True, raising=False)
    db = AsyncMock(); db.execute = _sess_and_nodes()
    resp = types.SimpleNamespace(
        text="", success=True, error=None,
        tool_calls=[ToolCall(id="t", name="report_progress", arguments={
            "verdict": "on_step", "confidence": 0.8,
            "reason": "just a question about the current install step"})])
    with patch.object(assist_agent, "get_step_recap", new=AsyncMock(return_value="")), \
         patch.object(assist_agent, "_history_or_transcript", new=AsyncMock(return_value=[])), \
         patch("app.model_router.tool_call", new=AsyncMock(return_value=resp)):
        out = await assist_tracker.assess_progress(
            session_id="s", message="which disk should I pick on this screen?", db=db)
    assert out["verdict"] == "on_step"
    assert out["new_step_request"] == ""


@pytest.mark.asyncio
async def test_tracker_failsoft_on_model_error(monkeypatch):
    monkeypatch.setattr(settings, "assist_progress_tracker_enabled", True, raising=False)
    db = AsyncMock(); db.execute = _sess_and_nodes()
    with patch.object(assist_agent, "get_step_recap", new=AsyncMock(return_value="")), \
         patch.object(assist_agent, "_history_or_transcript", new=AsyncMock(return_value=[])), \
         patch("app.model_router.tool_call", new=AsyncMock(side_effect=RuntimeError("down"))):
        out = await assist_tracker.assess_progress(
            session_id="s", message="help me with the networking please", db=db)
    assert out["verdict"] == "on_step" and out["reason"] == "tracker_unavailable"


@pytest.mark.asyncio
async def test_tracker_no_current_step(monkeypatch):
    monkeypatch.setattr(settings, "assist_progress_tracker_enabled", True, raising=False)
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _result(first_={"job_id": "j1", "status": "active",
                        "current_node_key": None, "metadata": {}}),
        _result(all_=[{"node_key": "T1", "title": "x", "status": "pending"}]),
    ])
    out = await assist_tracker.assess_progress(session_id="s", message="help me here now", db=db)
    assert out["verdict"] == "on_step" and out["reason"] == "no_current_step"


# ── §17.766: the /track ROUTER endpoint must FINALIZE when the tracker retires
# the last step (else the session hangs 'active' and the job never completes —
# the stuck-at-completion the §17.765 tracker-advance path made reachable). ──


def _track_db_prior(job_id="j1", node_key="T13"):
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result(
        first_={"job_id": job_id, "current_node_key": node_key}))
    return db


@pytest.mark.asyncio
async def test_track_advance_finalizes_on_last_step(monkeypatch):
    from app.routers import assist as assist_router
    monkeypatch.setattr(settings, "assist_tracker_confidence", 0.5, raising=False)
    db = _track_db_prior()
    body = types.SimpleNamespace(message="ok that whole install is done and I logged in")
    fin = AsyncMock()
    with patch.object(assist_router.assist_agent, "get_session",
                      new=AsyncMock(side_effect=[{"status": "active"},          # entry guard
                                                 {"status": "completed"}])), \
         patch("app.modules.assist_tracker.assess_progress",
                      new=AsyncMock(return_value={"verdict": "advance", "confidence": 0.99,
                                                  "current_step_done": True})), \
         patch.object(assist_router, "_retire_step_mirrored", new=AsyncMock()) as retire, \
         patch.object(assist_router.assist_agent, "_maybe_finalize_session", new=fin):
        out = await assist_router.assist_track("s1", body, db=db)
    retire.assert_awaited_once()
    fin.assert_awaited_once()                       # §17.766 — finalize was invoked
    assert out["action"] == "finalized"             # → pipeline shows the completion
    assert out["session_finalized"] is True


@pytest.mark.asyncio
async def test_track_advance_midplan_does_not_finalize(monkeypatch):
    # Same advance, but the session is still 'active' after retire (steps remain):
    # action stays 'advanced' and the caller presents the next step.
    from app.routers import assist as assist_router
    monkeypatch.setattr(settings, "assist_tracker_confidence", 0.5, raising=False)
    db = _track_db_prior()
    body = types.SimpleNamespace(message="done with this one, on to the next")
    with patch.object(assist_router.assist_agent, "get_session",
                      new=AsyncMock(side_effect=[{"status": "active"},
                                                 {"status": "active"}])), \
         patch("app.modules.assist_tracker.assess_progress",
                      new=AsyncMock(return_value={"verdict": "advance", "confidence": 0.99,
                                                  "current_step_done": True})), \
         patch.object(assist_router, "_retire_step_mirrored", new=AsyncMock()), \
         patch.object(assist_router.assist_agent, "_maybe_finalize_session",
                      new=AsyncMock()) as fin:
        out = await assist_router.assist_track("s1", body, db=db)
    fin.assert_awaited_once()                       # still called (idempotent no-op)
    assert out["action"] == "advanced"
    assert "session_finalized" not in out
