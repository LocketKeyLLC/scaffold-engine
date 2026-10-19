"""§17.1685 — a Shell step is never recorded done as runbook prose because the runner did not answer (live, job c2518102:
the Proxmox host went down mid-run, and M1–M3 were written up as runbooks and marked done)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import execution_agent as ea
from app.modules import supervised_runs as sr

NODE = {"id": "00000000-0000-0000-0000-000000000001", "node_key": "M1", "title": "Fetch Sonarr series details"}


def _db():
    db = AsyncMock()
    calls = []

    async def execute(stmt, params=None):
        calls.append(str(stmt))
        r = MagicMock(); r.scalar = MagicMock(return_value={})
        return r
    db.execute = execute
    db.commit = AsyncMock()
    return db, calls


@pytest.mark.asyncio
async def test_an_unreachable_runner_holds_the_step():
    db, calls = _db()
    with patch.object(sr, "channel", AsyncMock(return_value=None)), \
         patch.object(sr, "channel_state", AsyncMock(return_value=("unreachable", "pve-runner"))):
        out = await ea._hand_back_for_approval(db, "j", NODE, "Shell")
    assert out and out["status"] == "needs_approval" and out["node_key"] == "M1"
    assert any("SET status = 'pending'" in c for c in calls)              # back to pending, not done


@pytest.mark.asyncio
async def test_no_runner_at_all_keeps_the_old_path():
    db, _ = _db()
    for state in ("none", "off"):
        with patch.object(sr, "channel", AsyncMock(return_value=None)), \
             patch.object(sr, "channel_state", AsyncMock(return_value=(state, ""))):
            assert await ea._hand_back_for_approval(db, "j", NODE, "Shell") is None
