"""§17.693 — semantic pivot detection (detect_reroute).

The reported failure: on a fresh-reinstall plan the operator wrote "i already
have proxmox VE installed… we only need to remove old containers and start new."
Lexically unremarkable → the classifier mis-routed it to `skip`, and the plan
marched on with now-irrelevant reinstall steps. detect_reroute runs the §17.677
impact analyzer over the pending plan; when it invalidates steps it records a
decision note + stages a pending_replan, else it's a pure dry run (None).

The model / analyzer is always mocked.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings as _settings
from app.modules import assist_agent


def _result(mappings_first=None):
    r = MagicMock()
    m = MagicMock()
    m.first.return_value = mappings_first
    r.mappings.return_value = m
    return r


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setattr(_settings, "assist_pivot_detect_enabled", True)
    monkeypatch.setattr(_settings, "assist_note_replan_enabled", True)


@pytest.mark.asyncio
async def test_reroute_stages_replan_when_steps_affected():
    db = AsyncMock()
    db.execute.return_value = _result(mappings_first={"job_id": "j1", "status": "active"})
    affected = [
        {"node_key": "T3", "action": "drop", "current_assumption": "download ISO",
         "proposed_change": "already installed"},
        {"node_key": "T4", "action": "drop", "current_assumption": "make USB",
         "proposed_change": "no reinstall needed"},
    ]
    with patch("app.modules.assist_replan.analyze_note_impact",
               new=AsyncMock(return_value={"affected": affected})), \
         patch.object(assist_agent, "record_note",
                      new=AsyncMock(return_value={"kind": "decision"})) as note, \
         patch.object(assist_agent, "_stage_replan_proposal",
                      new=AsyncMock(return_value={"proposals": affected})) as stage:
        out = await assist_agent.detect_reroute(
            session_id="s1",
            message="i already have proxmox installed, we only need to remove old containers",
            db=db,
        )
    assert out == {"proposals": affected}
    note.assert_awaited_once()                       # recorded as feed-forward
    assert note.call_args.kwargs["kind"] == "decision"
    stage.assert_awaited_once()


@pytest.mark.asyncio
async def test_reroute_dry_run_when_nothing_affected():
    db = AsyncMock()
    db.execute.return_value = _result(mappings_first={"job_id": "j1", "status": "active"})
    with patch("app.modules.assist_replan.analyze_note_impact",
               new=AsyncMock(return_value={"affected": []})), \
         patch.object(assist_agent, "record_note", new=AsyncMock()) as note, \
         patch.object(assist_agent, "_stage_replan_proposal", new=AsyncMock()) as stage:
        out = await assist_agent.detect_reroute(
            session_id="s1", message="skip this, i did it by hand earlier", db=db,
        )
    assert out is None            # pure dry run
    note.assert_not_awaited()     # NO side effects
    stage.assert_not_awaited()


@pytest.mark.asyncio
async def test_reroute_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(_settings, "assist_pivot_detect_enabled", False)
    db = AsyncMock()
    with patch("app.modules.assist_replan.analyze_note_impact", new=AsyncMock()) as az:
        out = await assist_agent.detect_reroute(session_id="s1", message="anything at all here", db=db)
    assert out is None
    az.assert_not_awaited()       # gated before any analysis


@pytest.mark.asyncio
async def test_reroute_inactive_session_returns_none():
    db = AsyncMock()
    db.execute.return_value = _result(mappings_first={"job_id": "j1", "status": "completed"})
    with patch("app.modules.assist_replan.analyze_note_impact", new=AsyncMock()) as az:
        out = await assist_agent.detect_reroute(session_id="s1", message="a longer message here now", db=db)
    assert out is None
    az.assert_not_awaited()


@pytest.mark.asyncio
async def test_reroute_failsoft_on_analyzer_error():
    db = AsyncMock()
    db.execute.return_value = _result(mappings_first={"job_id": "j1", "status": "active"})
    with patch("app.modules.assist_replan.analyze_note_impact",
               new=AsyncMock(side_effect=RuntimeError("model down"))):
        out = await assist_agent.detect_reroute(
            session_id="s1", message="i already have this working a different way now", db=db,
        )
    assert out is None            # error → never trap the turn
