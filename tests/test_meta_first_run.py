"""§17.817 — first-run state + /models/available (plan 5.7 wizard backend)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.routers import meta as m
from app.routers import models as models_router


def _db_scalars(values):
    """db.execute(...).scalar() returns values in order."""
    db = AsyncMock()
    results = []
    for v in values:
        r = MagicMock()
        r.scalar.return_value = v
        results.append(r)
    db.execute = AsyncMock(side_effect=results)
    return db


@pytest.mark.asyncio
async def test_first_run_flag_wins():
    db = _db_scalars([{"completed": True}])
    out = await m.get_first_run(db=db)
    assert out == {"first_run": False, "source": "flag"}


@pytest.mark.asyncio
async def test_first_run_heuristic_fresh_install():
    """No flag + zero jobs + zero overrides → first run."""
    db = _db_scalars([None, 0, 0])
    out = await m.get_first_run(db=db)
    assert out == {"first_run": True, "source": "heuristic"}


@pytest.mark.asyncio
async def test_first_run_heuristic_exempts_used_install():
    """An engine with jobs must NEVER nag its operator with the wizard —
    even before the flag has ever been written (upgrade path)."""
    db = _db_scalars([None, 42, 0])
    out = await m.get_first_run(db=db)
    assert out["first_run"] is False


@pytest.mark.asyncio
async def test_complete_first_run_upserts_flag():
    db = AsyncMock()
    out = await m.complete_first_run(db=db)
    assert out["first_run"] is False
    sql = str(db.execute.call_args.args[0])
    assert "ON CONFLICT" in sql and "system_flags" in sql
    db.commit.assert_awaited_once()


# ── /models/available ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_available_splits_local_and_cloud():
    with patch.object(models_router, "_pulled_tags",
                      new=AsyncMock(return_value={"qwen3.5:latest", "kimi-k2.6:cloud",
                                                  "nomic-embed-text"})):
        out = await models_router.get_available_models()
    assert out["reachable"] is True
    assert out["cloud"] == ["kimi-k2.6:cloud"]
    assert out["local"] == ["nomic-embed-text", "qwen3.5:latest"]


@pytest.mark.asyncio
async def test_available_unreachable_daemon():
    with patch.object(models_router, "_pulled_tags", new=AsyncMock(return_value=None)):
        out = await models_router.get_available_models()
    assert out["reachable"] is False and out["local"] == [] and out["cloud"] == []
