"""§17.2042 — the lab's fill stream, started with no Alpaca keys, waits for them instead of dying unobserved.

Before: the entry started `track_fills()` as a background task nobody awaits; with no keys it raised at once
("Task exception was never retrieved") and stayed dead after the keys were stored, until the engine restarted.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import paper_fill_tracker as ft

KEYS = {ft.ALPACA_API_KEY_ID_SECRET_NAME: "kid", ft.ALPACA_API_SECRET_KEY_SECRET_NAME: "sk"}


@asynccontextmanager
async def _session():
    yield MagicMock()


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(ft, "SECRETS_POLL_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(ft, "RECONNECT_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(ft, "MAX_BACKOFF_SECONDS", 0.04, raising=False)


_real_sleep = asyncio.sleep          # bound before any test patches asyncio.sleep, so polling is never counted


async def _settle(task, until, limit=2.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + limit
    while not until() and not task.done() and loop.time() < end:
        await _real_sleep(0.01)


@pytest.mark.asyncio
async def test_the_entry_s_stream_task_lives_with_no_keys_and_says_so_once(fast, caplog):
    from app.modules import paper_wire_engine_entry_module as entry
    lookups = AsyncMock(return_value={})
    with patch.object(ft, "values_for", lookups), patch.object(ft, "async_session", _session), \
            caplog.at_level(logging.WARNING, logger="scaffold"):
        task = entry._start_stream_consumer()
        await _settle(task, lambda: lookups.await_count >= 3)
        assert not task.done(), task.exception() if task.done() else None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert lookups.await_count >= 3                       # the keys are re-read, not given up on
    unavailable = [r for r in caplog.records if "paper_fill_tracker_unavailable" in r.getMessage()]
    assert len(unavailable) == 1 and ft.ALPACA_API_KEY_ID_SECRET_NAME in unavailable[0].getMessage()


@pytest.mark.asyncio
async def test_storing_the_keys_starts_the_stream_without_a_restart(fast):
    lookups = AsyncMock(side_effect=[{}, {}, KEYS, KEYS, KEYS, KEYS, KEYS, KEYS, KEYS, KEYS])
    connect = MagicMock(side_effect=OSError("network unreachable"))
    with patch.object(ft, "values_for", lookups), patch.object(ft, "async_session", _session), \
            patch.object(ft.websockets, "connect", connect):
        task = asyncio.get_running_loop().create_task(ft.run_fill_tracker())
        await _settle(task, lambda: connect.call_count >= 2)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert connect.call_count >= 2                        # keys stored on the third read: the stream is dialled, and redialled
    assert connect.call_args.args[0] == ft.PAPER_STREAM_URL


@pytest.mark.asyncio
async def test_a_rejected_key_and_a_crash_are_logged_and_retried_with_a_backoff(fast, caplog, monkeypatch):
    calls = []
    outcomes = [ft.FillTrackerAuthError("Alpaca stream auth failed"), RuntimeError("boom"), None]

    async def _track():
        calls.append(1)
        if len(calls) <= len(outcomes):
            o = outcomes[len(calls) - 1]
            if o is not None:
                raise o
            return None                                    # the stream closed normally
        await asyncio.Event().wait()

    sleeps: list[float] = []

    async def _sleep(s):
        sleeps.append(s)
        await _real_sleep(0)
    monkeypatch.setattr(ft, "track_fills", _track)
    monkeypatch.setattr(ft.asyncio, "sleep", _sleep)
    with caplog.at_level(logging.ERROR, logger="scaffold"):
        task = asyncio.get_running_loop().create_task(ft.run_fill_tracker())
        await _settle(task, lambda: len(calls) >= 4)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    text = caplog.text
    assert "paper_fill_tracker_auth_failed" in text and "paper_fill_tracker_failed" in text
    assert sleeps[:3] == [0.01, 0.02, 0.01]                # backoff doubles; a normal close reconnects at the base


def test_missing_keys_are_still_an_auth_error_to_track_fills_callers():
    assert issubclass(ft.FillTrackerSecretsMissing, ft.FillTrackerAuthError)
