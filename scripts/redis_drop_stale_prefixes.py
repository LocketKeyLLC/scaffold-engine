"""Drop stale cache-prefix keyspaces from Redis (§17.139).

Cache modules in this repo version their key prefixes so a contract
change auto-invalidates: ``embedv2`` → ``embedv3`` (§9.25),
``ragv1`` (§17.129), ``llmverifyv1`` (§17.128), ``fetchv1`` (§17.117).
Once a new prefix ships, the old one's keys sit in Redis taking up
memory until natural TTL expiry — which can be days or weeks
depending on the cache. This script SCAN+DELs them on demand so a
version bump is a one-command cleanup instead of a wait.

Usage (run inside the orchestrator container so Redis is reachable
via the bridge network)::

    docker exec -it scaffold-orchestrator python scripts/redis_drop_stale_prefixes.py [flags] <prefix> [<prefix> ...]

    --dry-run             Count keys but don't delete (always exits 0).
    --batch <n>           DELETE batch size (default 500).
    --redis-url <url>     Override settings.redis_url (rare).

Allowlist: only the explicit cache prefixes below are accepted. A
typo like ``embed`` (missing version) or an unrelated prefix like
``sessions`` is rejected before any SCAN runs so an operator can't
accidentally blow away the wrong keyspace. Add a new prefix to
``ALLOWED_PREFIXES`` when you ship a new cache module.

Exit codes:
  0  success (including --dry-run)
  1  argument / allowlist error
  2  unknown prefix (separate code so operators can grep this in CI)
  3  Redis error during SCAN or DELETE
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Iterable

import redis.asyncio as aioredis

from app.config import settings

logger = logging.getLogger("scaffold.redis_drop_stale_prefixes")

# Allowlist of every cache-key prefix this repo defines. Bump on each
# new cache module ship. The allowlist gate keeps a typo'd prefix from
# nuking unrelated keys (apscheduler_jobs, etc.). Old prefixes stay in
# the list forever so post-upgrade cleanup remains possible.
ALLOWED_PREFIXES: frozenset[str] = frozenset({
    "embedv1",        # legacy embedding cache (pre-§9.25)
    "embedv2",        # legacy embedding cache (pre-§9.25 dim-keyed bump)
    "embedv3",        # current embedding cache (§9.25)
    "fetchv1",        # upstream HTTP body cache (§17.117)
    "llmverifyv1",    # verifier-verdict cache (§17.128)
    "ragv1",          # RAG result cache (§17.129)
})


async def _scan_count_and_delete(
    client: aioredis.Redis,
    prefix: str,
    *,
    dry_run: bool,
    batch_size: int,
) -> tuple[int, int]:
    """Walk Redis once, accumulating + deleting keys matching `prefix:*`.

    Returns ``(scanned, deleted)``. In dry-run mode ``deleted`` is always 0.
    Batches DELETEs to avoid sending a 1M-element ``UNLINK`` in one round-trip
    on a large cache.
    """
    pattern = f"{prefix}:*"
    scanned = 0
    deleted = 0
    buffer: list = []

    async def _flush() -> None:
        nonlocal deleted, buffer
        if not buffer:
            return
        if dry_run:
            buffer = []
            return
        try:
            n = await client.unlink(*buffer)
        except Exception:
            # Fallback for very old servers that don't have UNLINK.
            n = await client.delete(*buffer)
        deleted += int(n or 0)
        buffer = []

    async for key in client.scan_iter(match=pattern, count=max(batch_size, 100)):
        scanned += 1
        buffer.append(key)
        if len(buffer) >= batch_size:
            await _flush()
        if scanned % (batch_size * 10) == 0:
            logger.info(
                "redis_drop_progress: prefix=%s scanned=%d deleted=%d dry_run=%s",
                prefix, scanned, deleted, dry_run,
            )
    await _flush()
    return scanned, deleted


async def _drop_prefixes(
    prefixes: Iterable[str],
    *,
    dry_run: bool,
    batch_size: int,
    redis_url: str,
) -> int:
    """Coordinate the drop across multiple prefixes. Returns an exit code."""
    client = aioredis.from_url(redis_url, decode_responses=False)
    try:
        try:
            await client.ping()
        except Exception as exc:
            logger.error("redis_unreachable: url=%s err=%s", redis_url, exc)
            return 3

        total_scanned = 0
        total_deleted = 0
        for prefix in prefixes:
            logger.info(
                "redis_drop_start: prefix=%s dry_run=%s batch=%d",
                prefix, dry_run, batch_size,
            )
            try:
                scanned, deleted = await _scan_count_and_delete(
                    client, prefix, dry_run=dry_run, batch_size=batch_size,
                )
            except Exception as exc:
                logger.error("redis_drop_failed: prefix=%s err=%s", prefix, exc)
                return 3
            logger.info(
                "redis_drop_done: prefix=%s scanned=%d deleted=%d dry_run=%s",
                prefix, scanned, deleted, dry_run,
            )
            total_scanned += scanned
            total_deleted += deleted

        # Summary line that's easy to grep.
        logger.info(
            "redis_drop_summary: prefixes=%d total_scanned=%d total_deleted=%d dry_run=%s",
            sum(1 for _ in prefixes),  # cheap — prefixes was a list/tuple
            total_scanned, total_deleted, dry_run,
        )
        return 0
    finally:
        try:
            await client.aclose()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scripts/redis_drop_stale_prefixes.py",
        description=(
            "SCAN+DEL Redis keys under a cache-key prefix. Use after a "
            "cache contract bump (e.g. embedv2 → embedv3) to release "
            "memory immediately instead of waiting for TTL expiry."
        ),
    )
    p.add_argument(
        "prefixes",
        nargs="+",
        help=(
            "One or more cache-key prefixes to drop. Must appear in "
            f"the allowlist: {', '.join(sorted(ALLOWED_PREFIXES))}."
        ),
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Count keys without deleting. Exits 0.",
    )
    p.add_argument(
        "--batch", type=int, default=500,
        help="DELETE batch size (default 500). Higher = fewer round-trips.",
    )
    p.add_argument(
        "--redis-url", default=None,
        help="Override settings.redis_url (rare).",
    )
    return p


def _validate_prefixes(prefixes: list[str]) -> tuple[list[str], list[str]]:
    """Partition supplied prefixes into (allowed, unknown). Order preserved."""
    allowed: list[str] = []
    unknown: list[str] = []
    for p in prefixes:
        if p in ALLOWED_PREFIXES:
            allowed.append(p)
        else:
            unknown.append(p)
    return allowed, unknown


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    ns = _build_arg_parser().parse_args(argv)
    if ns.batch < 1:
        sys.stderr.write("--batch must be >= 1\n")
        return 1

    allowed, unknown = _validate_prefixes(ns.prefixes)
    if unknown:
        sys.stderr.write(
            f"unknown prefix(es) not in allowlist: {unknown}\n"
            f"allowed: {sorted(ALLOWED_PREFIXES)}\n"
        )
        return 2

    redis_url = ns.redis_url or settings.redis_url
    return asyncio.run(_drop_prefixes(
        allowed, dry_run=ns.dry_run, batch_size=ns.batch, redis_url=redis_url,
    ))


if __name__ == "__main__":
    sys.exit(main())
