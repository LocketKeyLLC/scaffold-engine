"""§17.574 — bounded component concurrency (get_component_sem).

run_component_pipeline acquires a module semaphore sized by
settings.decompose_component_max_concurrent; components beyond the cap queue.
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.modules import decomposition as dec


class _DummyCtx:
    async def __aenter__(self):
        return AsyncMock()

    async def __aexit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def _reset_sem():
    dec._reset_component_sem()
    yield
    dec._reset_component_sem()


@pytest.mark.asyncio
async def test_component_pipeline_concurrency_capped(monkeypatch):
    monkeypatch.setattr(settings, "decompose_component_max_concurrent", 2)
    dec._reset_component_sem()

    active = 0
    peak = 0
    release = asyncio.Event()

    async def fake_phase1(*a, **k):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await release.wait()      # hold the component slot
        active -= 1
        return {"status": "failed"}   # early return → skip phase2/exec

    monkeypatch.setattr(dec, "analyze_and_confirm", fake_phase1)
    monkeypatch.setattr(dec, "async_session", lambda: _DummyCtx())
    monkeypatch.setattr(dec, "get_ideation_slot_sem", lambda: asyncio.Semaphore(100))
    monkeypatch.setattr(dec, "_rollup_umbrella", AsyncMock())

    tasks = [
        asyncio.create_task(dec.run_component_pipeline(
            f"c{i}", "idea", domain=None, research_queries=None,
            model_overrides=None, umbrella_id="u",
        ))
        for i in range(4)
    ]
    await asyncio.sleep(0.15)           # let all 4 race to the semaphore
    assert active == 2, f"cap=2 but {active} components active at once"

    release.set()
    await asyncio.gather(*tasks)
    assert peak == 2                    # 4 spawned, never more than the cap ran


@pytest.mark.asyncio
async def test_component_sem_serial_when_cap_one(monkeypatch):
    monkeypatch.setattr(settings, "decompose_component_max_concurrent", 1)
    dec._reset_component_sem()
    peak = 0
    active = 0
    release = asyncio.Event()

    async def fake_phase1(*a, **k):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await release.wait()
        active -= 1
        return {"status": "failed"}

    monkeypatch.setattr(dec, "analyze_and_confirm", fake_phase1)
    monkeypatch.setattr(dec, "async_session", lambda: _DummyCtx())
    monkeypatch.setattr(dec, "get_ideation_slot_sem", lambda: asyncio.Semaphore(100))
    monkeypatch.setattr(dec, "_rollup_umbrella", AsyncMock())

    tasks = [
        asyncio.create_task(dec.run_component_pipeline(
            f"c{i}", "idea", domain=None, research_queries=None,
            model_overrides=None, umbrella_id="u",
        ))
        for i in range(3)
    ]
    await asyncio.sleep(0.15)
    assert active == 1                  # cap=1 → strictly serial
    release.set()
    await asyncio.gather(*tasks)
    assert peak == 1


@pytest.mark.asyncio
async def test_cancelled_component_releases_slot(monkeypatch):
    """§17.854 (audit A8) — a CancelledError propagating out of the shielded
    rollup in the finally must NOT leak the semaphore slot. Before the fix the
    release sat after an `except Exception` that can't catch CancelledError, so
    the slot was lost until process restart. We drive the exact path by having
    the rollup raise CancelledError (as a real cancel-during-await would)."""
    monkeypatch.setattr(settings, "decompose_component_max_concurrent", 1)
    dec._reset_component_sem()

    async def fake_phase1(*a, **k):
        return {"status": "failed"}   # early return → straight to the finally

    async def cancelling_rollup(*a, **k):
        raise asyncio.CancelledError()

    monkeypatch.setattr(dec, "analyze_and_confirm", fake_phase1)
    monkeypatch.setattr(dec, "async_session", lambda: _DummyCtx())
    monkeypatch.setattr(dec, "get_ideation_slot_sem", lambda: asyncio.Semaphore(100))
    monkeypatch.setattr(dec, "_rollup_umbrella", cancelling_rollup)

    task = asyncio.create_task(dec.run_component_pipeline(
        "c0", "idea", domain=None, research_queries=None,
        model_overrides=None, umbrella_id="u",
    ))
    # The CancelledError from the shielded rollup re-raises out of the pipeline.
    with pytest.raises(asyncio.CancelledError):
        await task

    # The slot must be free despite the cancellation: a fresh acquire is instant.
    sem = dec.get_component_sem()
    await asyncio.wait_for(sem.acquire(), timeout=1.0)
    sem.release()
