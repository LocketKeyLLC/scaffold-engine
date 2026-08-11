"""§17.761 — reconnect orientation: /assist/start attaches a WHERE-YOU-ARE
snapshot; the pipeline leads the reconnect with it instead of a raw step dump, and
the step header no longer shows a bare "Step N" that reads as a node key.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.modules import assist_agent


def _r(first=None, all_=None):
    m = MagicMock()
    m.mappings.return_value.first.return_value = first
    m.mappings.return_value.all.return_value = all_ if all_ is not None else []
    return m


@pytest.mark.asyncio
async def test_orientation_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(settings, "assist_reconnect_orientation_enabled", False, raising=False)
    assert await assist_agent.build_reconnect_orientation(session_id="s", db=AsyncMock()) is None


@pytest.mark.asyncio
async def test_orientation_snapshot(monkeypatch):
    monkeypatch.setattr(settings, "assist_reconnect_orientation_enabled", True, raising=False)
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _r(first={"job_id": "j1", "current_node_key": "ADD2"}),                       # session
        _r(first={"title": "DeFruscio HomeLab — P40", "project_recap": "GOAL: build"}),  # job
        _r(all_=[
            {"node_key": "T11", "title": "Create VM", "status": "done"},
            {"node_key": "T12", "title": "Attach P40", "status": "done"},
            {"node_key": "T13", "title": "Install guest OS", "status": "skipped"},
            {"node_key": "ADD2", "title": "Configure guest network", "status": "pending"},
            {"node_key": "T14", "title": "Install NVIDIA driver", "status": "pending"},
            {"node_key": "T15", "title": "Install CUDA", "status": "pending"},
        ]),
    ])
    o = await assist_agent.build_reconnect_orientation(session_id="s", db=db)
    assert o["job_title"] == "DeFruscio HomeLab — P40"
    assert o["done_n"] == 3 and o["total_n"] == 6          # T11,T12 done + T13 skipped
    assert o["current_title"] == "Configure guest network"
    assert o["current_key"] == "ADD2"
    assert o["done_recent"][-1] == "Install guest OS"       # most recent done/skipped
    assert o["upcoming"] == ["Install NVIDIA driver", "Install CUDA"]  # current excluded
    assert o["project_recap"] == "GOAL: build"


def test_pipeline_renders_orientation_panel():
    from tests._scaffold_router_setup import _mod as _router_mod
    _vendor = _router_mod._assist
    out = _vendor.render_reconnect_orientation(
        {"job_title": "DeFruscio HomeLab — P40", "done_n": 14, "total_n": 23,
         "current_title": "Configure guest network", "current_key": "ADD2",
         "done_recent": ["Attach P40", "Install guest OS"],
         "upcoming": ["Install NVIDIA driver", "Install CUDA"],
         "project_recap": "GOAL: homelab\nDECISIONS: use ZFS"},
        "sid-1", "job-1")
    assert "📍" in out and "Picking up: DeFruscio HomeLab — P40" in out
    assert "14 of 23 steps done" in out
    assert "Now:** Configure guest network" in out
    assert "Install NVIDIA driver" in out                   # what's next
    assert "Where the whole project stands" in out          # recap collapsed in
    # de-collision: no bare "Step 15"-style ordinal that reads as a node key
    assert "Step 15" not in out
