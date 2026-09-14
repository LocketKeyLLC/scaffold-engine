"""§17.1069 — per-host pacing and cooldown."""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.modules import fetch_pacing as fp
from app.modules import research_extractors as rx


@pytest.mark.asyncio
async def test_same_host_requests_are_spaced_and_other_hosts_are_not():
    p = fp.HostPacer(min_interval_s=0.2)
    t0 = time.monotonic()
    await p.acquire("https://a.example/1"); await p.acquire("https://a.example/2"); await p.acquire("https://a.example/3")
    same = time.monotonic() - t0
    t1 = time.monotonic()
    await p.acquire("https://b.example/1"); await p.acquire("https://c.example/1")
    other = time.monotonic() - t1
    assert same >= 0.38 and other < 0.15


@pytest.mark.asyncio
async def test_403_puts_the_host_in_a_growing_cooldown_and_200_clears_it():
    p = fp.HostPacer(min_interval_s=0, base_cooldown_s=0.2, max_cooldown_s=1.0)
    p.record("https://so.example/q", 403)
    assert p.cooldown_remaining("so.example") > 0.1
    assert await p.acquire("https://so.example/q2") is None          # refused up front
    assert await p.acquire("https://ok.example/x") == 0.0             # other hosts unaffected
    p.record("https://so.example/q", 403)
    assert 0.3 < p.cooldown_remaining("so.example") <= 0.4            # doubled
    p.record("https://so.example/q", 200)
    assert p.cooldown_remaining("so.example") == 0.0 and p.snapshot() == {}


class _Stream:
    def __init__(self, status): self.status_code = status; self.url = "https://so.example/p"; self.headers = {}; self.encoding = "utf-8"
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def aiter_bytes(self):
        yield b"ok"


@pytest.mark.asyncio
async def test_fetch_records_status_and_refuses_during_cooldown():
    fp.reset_pacer()
    ra = MagicMock(); ra.get_generic_http_client.return_value = MagicMock(stream=MagicMock(return_value=_Stream(403)))
    with patch.object(rx, "_ra", return_value=ra), patch.object(rx, "_is_public_host", return_value=(True, "")), \
         patch.object(settings, "research_fetch_impersonate_enabled", False), \
         patch.object(settings, "research_fetch_host_min_interval_s", 0.0), \
         patch.object(settings, "research_fetch_host_cooldown_s", 5.0):
        f1 = {}; assert await rx._fetch_url_bounded("https://so.example/p", failure=f1) is None and f1["reason"] == "http_403"
        f2 = {}; assert await rx._fetch_url_bounded("https://so.example/p2", failure=f2) is None and f2["reason"] == "host_cooldown"
        ra.get_generic_http_client.return_value.stream.assert_called_once()  # the second fetch never left the process
    fp.reset_pacer()
