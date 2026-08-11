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
