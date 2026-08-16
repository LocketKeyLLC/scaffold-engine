"""§17.801 — _fetch_and_extract must never run more than
settings.research_fetch_concurrency fetch+parse jobs at once.

The semaphore spans BOTH the HTTP fetch and the trafilatura/lxml parse, so this
peak is what bounds research memory. Regression guard for the §17.800 mid-run
cgroup memory-kill: a future refactor that drops/loosens the semaphore would
re-expose the OOM.
"""
import asyncio
from unittest.mock import patch

import pytest

from app.modules import research_agent as ra


@pytest.mark.asyncio
async def test_fetch_and_extract_respects_concurrency_bound(monkeypatch):
    monkeypatch.setattr(ra.settings, "research_fetch_concurrency", 3)

    live = 0
    peak = 0
    lock = asyncio.Lock()

    async def _fake_fetch(url, timeout=None):
        nonlocal live, peak
        async with lock:
            live += 1
            peak = max(peak, live)
        try:
            await asyncio.sleep(0.02)  # hold the slot so overlap is observable
            return f"<html>{url}</html>"
        finally:
            async with lock:
                live -= 1

    # trafilatura.extract runs in a thread; return enough text to pass the
    # >=100-char keep filter so the row is retained.
    with patch.object(ra, "_fetch_url_bounded", side_effect=_fake_fetch), \
         patch.object(ra.trafilatura, "extract", return_value="x" * 200):
        results = [{"url": f"https://example.com/{i}"} for i in range(12)]
        out = await ra._fetch_and_extract(results)

    assert len(out) == 12                      # all fetched+extracted
    assert peak <= 3, f"peak concurrency {peak} exceeded the bound of 3"
    assert peak > 1, "test did not actually exercise concurrency"


@pytest.mark.asyncio
async def test_fetch_and_extract_bound_of_one_serializes(monkeypatch):
    """concurrency=1 → strictly serial (peak never exceeds 1)."""
    monkeypatch.setattr(ra.settings, "research_fetch_concurrency", 1)

    live = 0
    peak = 0
    lock = asyncio.Lock()

    async def _fake_fetch(url, timeout=None):
        nonlocal live, peak
        async with lock:
            live += 1
            peak = max(peak, live)
        try:
            await asyncio.sleep(0.01)
            return "<html>ok</html>"
        finally:
            async with lock:
                live -= 1

    with patch.object(ra, "_fetch_url_bounded", side_effect=_fake_fetch), \
         patch.object(ra.trafilatura, "extract", return_value="y" * 200):
        out = await ra._fetch_and_extract([{"url": f"https://e.com/{i}"} for i in range(5)])

    assert len(out) == 5
    assert peak == 1
