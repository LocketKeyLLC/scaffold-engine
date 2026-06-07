"""§17.442 — stress-test follow-ups.

  #4   Ideation concurrency cap — /ideas + /ideate had no concurrency bound
       (the §17.441 stress fired 6 concurrent /ideate, all hit the cloud at
       once). A router-layer asyncio.Semaphore now queues bursts.
  reaper margin — stale_threshold_minutes cap raised 1440 → 2880 so the reaper
       window can sit strictly ABOVE a 24h node_timeout (no overlap warning).
"""
import asyncio

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.modules import ideation_workflow as iw


# ───────────────────────────── #4 ideation cap ─────────────────────────────

@pytest.fixture(autouse=True)
def _reset_sem():
    iw._reset_ideation_slot_sem()
    yield
    iw._reset_ideation_slot_sem()


def test_semaphore_sized_to_settings(monkeypatch):
    monkeypatch.setattr(iw.settings, "ideation_global_concurrency", 3)
    iw._reset_ideation_slot_sem()
    sem = iw.get_ideation_slot_sem()
    assert isinstance(sem, asyncio.Semaphore)
    assert sem._value == 3
    # cached — same object on re-fetch
    assert iw.get_ideation_slot_sem() is sem


def test_reset_rereads_settings(monkeypatch):
    monkeypatch.setattr(iw.settings, "ideation_global_concurrency", 2)
    iw._reset_ideation_slot_sem()
    assert iw.get_ideation_slot_sem()._value == 2
    monkeypatch.setattr(iw.settings, "ideation_global_concurrency", 7)
    iw._reset_ideation_slot_sem()
    assert iw.get_ideation_slot_sem()._value == 7


@pytest.mark.asyncio
async def test_cap_bounds_concurrent_acquirers(monkeypatch):
    """With cap=2, no more than 2 coroutines hold the slot simultaneously even
    when 6 contend — the rest queue."""
    monkeypatch.setattr(iw.settings, "ideation_global_concurrency", 2)
    iw._reset_ideation_slot_sem()
    sem = iw.get_ideation_slot_sem()

    inflight = 0
    peak = 0

    async def worker():
        nonlocal inflight, peak
        async with sem:
            inflight += 1
            peak = max(peak, inflight)
            await asyncio.sleep(0.02)  # hold the slot
            inflight -= 1

    await asyncio.gather(*(worker() for _ in range(6)))
    assert peak <= 2, f"cap breached: {peak} concurrent (expected ≤ 2)"
    assert inflight == 0


# ───────────────────────────── reaper margin ─────────────────────────────

def test_stale_threshold_cap_raised_to_2880():
    # 2880 (48h) now accepted; 2881 still rejected.
    assert Settings(stale_threshold_minutes=2880).stale_threshold_minutes == 2880
    with pytest.raises(ValidationError):
        Settings(stale_threshold_minutes=2881)


def test_reaper_margin_clears_overlap_warning(caplog):
    # stale window (1560 min = 93600 s) now strictly exceeds a 24h node_timeout
    # (86400 s) → the config_timeout_reaper_overlap warning must NOT fire.
    import logging
    with caplog.at_level(logging.WARNING):
        Settings(stale_threshold_minutes=1560, node_timeout_seconds=86400)
    assert "config_timeout_reaper_overlap" not in caplog.text


def test_reaper_overlap_warning_still_fires_when_equal(caplog):
    # Regression guard: the warning must still fire on the old equal config.
    import logging
    with caplog.at_level(logging.WARNING):
        Settings(stale_threshold_minutes=1440, node_timeout_seconds=86400)
    assert "config_timeout_reaper_overlap" in caplog.text
