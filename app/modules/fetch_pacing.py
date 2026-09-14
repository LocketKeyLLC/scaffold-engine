"""§17.1069 — per-host pacing and backoff for research fetches.

The §17.1066 measurement: Stack Exchange answered this box's plain AND
browser-fingerprint requests with 403, and its API reported an IP-level
throttle ("too many requests from this IP, more requests available in
25445 seconds"). That throttle was EARNED — the fetcher's only bound was a
global concurrency of 3, so one research iteration could hit one host
thirty times inside a second. A fingerprint cannot fix a volume problem;
pacing can.

Two small mechanisms, process-local, no new service:
- a per-host minimum interval between request starts (default 0.75 s), so
  a burst against one host becomes a queue while other hosts proceed;
- a per-host cooldown after a 403/429 that grows on repeats (2 s → 4 s → …
  capped), during which fetches to that host are refused up front
  (``failure["reason"] = "host_cooldown"``) instead of adding to the count
  that keeps the throttle alive. A 200 clears the host's strike count.
"""
from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import urlsplit

logger = logging.getLogger("scaffold")


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""


class HostPacer:
    def __init__(self, *, min_interval_s: float = 0.75, base_cooldown_s: float = 2.0,
                 max_cooldown_s: float = 120.0) -> None:
        self.min_interval_s = min_interval_s
        self.base_cooldown_s = base_cooldown_s
        self.max_cooldown_s = max_cooldown_s
        self._next_ok: dict[str, float] = {}      # host → earliest monotonic time a request may START
        self._cooldown_until: dict[str, float] = {}
        self._strikes: dict[str, int] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, host: str) -> asyncio.Lock:
        lk = self._locks.get(host)
        if lk is None:
            lk = self._locks[host] = asyncio.Lock()
        return lk

    def cooldown_remaining(self, host: str) -> float:
        return max(0.0, self._cooldown_until.get(host, 0.0) - time.monotonic())

    async def acquire(self, url: str) -> float | None:
        """Wait for the host's turn. Returns the seconds waited, or None when the
        host is in cooldown (the caller must not fetch)."""
        host = host_of(url)
        if not host:
            return 0.0
        remaining = self.cooldown_remaining(host)
        if remaining > 0:
            return None
        async with self._lock(host):
            now = time.monotonic()
            wait = max(0.0, self._next_ok.get(host, 0.0) - now)
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_ok[host] = time.monotonic() + self.min_interval_s
            return wait

    def record(self, url: str, status: int | None) -> None:
        host = host_of(url)
        if not host:
            return
        if status in (403, 429):
            n = self._strikes.get(host, 0) + 1
            self._strikes[host] = n
            cd = min(self.max_cooldown_s, self.base_cooldown_s * (2 ** (n - 1)))
            self._cooldown_until[host] = time.monotonic() + cd
            logger.warning("fetch_host_cooldown: host=%s status=%s strikes=%d cooldown_s=%.0f", host, status, n, cd)
        elif status is not None and 200 <= status < 400:
            if self._strikes.pop(host, None):
                self._cooldown_until.pop(host, None)

    def snapshot(self) -> dict:
        now = time.monotonic()
        return {h: {"strikes": self._strikes.get(h, 0), "cooldown_s": round(max(0.0, t - now), 1)}
                for h, t in self._cooldown_until.items() if t > now}


_pacer: HostPacer | None = None


def get_pacer() -> HostPacer:
    global _pacer
    if _pacer is None:
        from app.config import settings
        _pacer = HostPacer(min_interval_s=settings.research_fetch_host_min_interval_s,
                           base_cooldown_s=settings.research_fetch_host_cooldown_s)
    return _pacer


def reset_pacer() -> None:
    global _pacer
    _pacer = None
