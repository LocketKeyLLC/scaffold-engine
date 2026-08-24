"""Tests for scripts/redis_drop_stale_prefixes.py (§17.139).

Covers:
  - argument parsing + allowlist gate
  - dry-run path counts without deleting
  - happy path scans + deletes
  - multi-prefix run accumulates totals
  - SCAN failure returns exit 3
  - Redis-unreachable returns exit 3
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# Make the scripts/ package importable in-test.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from scripts import redis_drop_stale_prefixes as drop_mod  # noqa: E402


# ---------------------------------------------------------------------------
# allowlist + arg parsing
# ---------------------------------------------------------------------------

def test_allowlist_contains_every_shipped_prefix():
    """Sanity: every cache-prefix this repo defines must be in the
    allowlist. Forgetting one would silently make this script unable
    to clean it up after a contract bump."""
    expected = {"embedv1", "embedv2", "embedv3", "embedv4", "fetchv1",
                "llmverifyv1", "ragv1"}
    assert expected <= drop_mod.ALLOWED_PREFIXES, (
        f"missing prefixes: {expected - drop_mod.ALLOWED_PREFIXES}"
    )


def test_validate_partitions_allowed_and_unknown():
    allowed, unknown = drop_mod._validate_prefixes(
        ["embedv3", "sessions", "ragv1", "typo"]
    )
    assert allowed == ["embedv3", "ragv1"]
    assert unknown == ["sessions", "typo"]


def test_unknown_prefix_returns_exit_2(capsys):
    rc = drop_mod.main(["embedv3", "sessions"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "sessions" in captured.err
    assert "allowed" in captured.err.lower()


def test_bad_batch_returns_exit_1(capsys):
    rc = drop_mod.main(["embedv3", "--batch", "0"])
    assert rc == 1
    assert "--batch" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# _scan_count_and_delete behavior — direct unit test
# ---------------------------------------------------------------------------

def _fake_scan_iter(keys: list[bytes]):
    async def _gen(match=None, count=None):
        for k in keys:
            yield k
    return _gen


@pytest.mark.asyncio
async def test_scan_delete_happy_path():
    keys = [f"embedv2:m:d512:k{i:03d}".encode() for i in range(7)]
    client = AsyncMock()
    client.scan_iter = _fake_scan_iter(keys)
    client.unlink = AsyncMock(return_value=7)

    scanned, deleted = await drop_mod._scan_count_and_delete(
        client, "embedv2", dry_run=False, batch_size=500,
    )
    assert scanned == 7
    assert deleted == 7
    # One UNLINK call for the entire batch (all 7 fit under batch=500).
    client.unlink.assert_awaited_once_with(*keys)


@pytest.mark.asyncio
async def test_scan_delete_batches():
    """With batch_size=2 and 5 keys we should see 3 UNLINK calls (2+2+1)."""
    keys = [f"embedv2:m:d512:k{i}".encode() for i in range(5)]
    client = AsyncMock()
    client.scan_iter = _fake_scan_iter(keys)
    # Each UNLINK call: return however many keys it received.
    async def _unlink(*ks):
        return len(ks)
    client.unlink = AsyncMock(side_effect=_unlink)

    scanned, deleted = await drop_mod._scan_count_and_delete(
        client, "embedv2", dry_run=False, batch_size=2,
    )
    assert scanned == 5
    assert deleted == 5
    assert client.unlink.await_count == 3


@pytest.mark.asyncio
async def test_dry_run_does_not_delete():
    keys = [f"embedv2:m:d512:k{i}".encode() for i in range(4)]
    client = AsyncMock()
    client.scan_iter = _fake_scan_iter(keys)
    client.unlink = AsyncMock()
    client.delete = AsyncMock()

    scanned, deleted = await drop_mod._scan_count_and_delete(
        client, "embedv2", dry_run=True, batch_size=500,
    )
    assert scanned == 4
    assert deleted == 0
    client.unlink.assert_not_awaited()
    client.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_prefix_yields_zero():
    client = AsyncMock()
    client.scan_iter = _fake_scan_iter([])
    client.unlink = AsyncMock()

    scanned, deleted = await drop_mod._scan_count_and_delete(
        client, "embedv2", dry_run=False, batch_size=500,
    )
    assert scanned == 0
    assert deleted == 0
    client.unlink.assert_not_awaited()


@pytest.mark.asyncio
async def test_unlink_falls_back_to_delete():
    """If UNLINK raises (very old Redis), fall back to DELETE."""
    keys = [b"embedv2:m:d512:a", b"embedv2:m:d512:b"]
    client = AsyncMock()
    client.scan_iter = _fake_scan_iter(keys)
    client.unlink = AsyncMock(side_effect=Exception("UNLINK not supported"))
    client.delete = AsyncMock(return_value=2)

    scanned, deleted = await drop_mod._scan_count_and_delete(
        client, "embedv2", dry_run=False, batch_size=500,
    )
    assert scanned == 2
    assert deleted == 2
    client.delete.assert_awaited_once_with(*keys)


# ---------------------------------------------------------------------------
# _drop_prefixes — end-to-end via patched from_url
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_drop_prefixes_redis_unreachable_returns_3():
    fake_client = AsyncMock()
    fake_client.ping = AsyncMock(side_effect=ConnectionError("nope"))
    fake_client.aclose = AsyncMock()
    with patch(
        "scripts.redis_drop_stale_prefixes.aioredis.from_url",
        return_value=fake_client,
    ):
        rc = await drop_mod._drop_prefixes(
            ["embedv2"], dry_run=False, batch_size=500,
            redis_url="redis://nope:6379/0",
        )
    assert rc == 3


@pytest.mark.asyncio
async def test_drop_prefixes_scan_failure_returns_3():
    fake_client = AsyncMock()
    fake_client.ping = AsyncMock()
    async def _failing_scan(match=None, count=None):
        raise ConnectionError("redis fell over mid-scan")
        yield
    fake_client.scan_iter = _failing_scan
    fake_client.aclose = AsyncMock()
    with patch(
        "scripts.redis_drop_stale_prefixes.aioredis.from_url",
        return_value=fake_client,
    ):
        rc = await drop_mod._drop_prefixes(
            ["embedv2"], dry_run=False, batch_size=500,
            redis_url="redis://x:6379/0",
        )
    assert rc == 3


@pytest.mark.asyncio
async def test_drop_prefixes_multi_prefix_accumulates(caplog):
    """Two prefixes, each with N keys → both get scanned and deleted."""
    # Track which prefix is being scanned via the match= arg.
    keys_by_prefix = {
        "embedv2": [b"embedv2:m:d512:a", b"embedv2:m:d512:b"],
        "embedv3": [b"embedv3:m:d512:c"],
    }

    fake_client = AsyncMock()
    fake_client.ping = AsyncMock()
    fake_client.aclose = AsyncMock()
    fake_client.unlink = AsyncMock(side_effect=lambda *ks: len(ks))

    async def _scan(match=None, count=None):
        prefix = match.split(":")[0]
        for k in keys_by_prefix.get(prefix, []):
            yield k
    fake_client.scan_iter = _scan

    with patch(
        "scripts.redis_drop_stale_prefixes.aioredis.from_url",
        return_value=fake_client,
    ):
        with caplog.at_level("INFO", logger="scaffold.redis_drop_stale_prefixes"):
            rc = await drop_mod._drop_prefixes(
                ["embedv2", "embedv3"], dry_run=False, batch_size=500,
                redis_url="redis://x:6379/0",
            )

    assert rc == 0
    # Each prefix produced one DONE line
    done_lines = [r.getMessage() for r in caplog.records if "redis_drop_done" in r.getMessage()]
    assert any("prefix=embedv2 scanned=2 deleted=2" in m for m in done_lines)
    assert any("prefix=embedv3 scanned=1 deleted=1" in m for m in done_lines)


# ---------------------------------------------------------------------------
# main() end-to-end with everything patched
# ---------------------------------------------------------------------------

def test_main_dry_run_exits_zero():
    """A dry-run on a valid prefix must always exit 0."""
    fake_client = AsyncMock()
    fake_client.ping = AsyncMock()
    fake_client.aclose = AsyncMock()
    fake_client.scan_iter = _fake_scan_iter(
        [b"embedv2:m:d512:a"]
    )
    fake_client.unlink = AsyncMock()
    with patch(
        "scripts.redis_drop_stale_prefixes.aioredis.from_url",
        return_value=fake_client,
    ):
        rc = drop_mod.main(["embedv2", "--dry-run"])
    assert rc == 0
    fake_client.unlink.assert_not_awaited()


def test_main_happy_path_exits_zero():
    fake_client = AsyncMock()
    fake_client.ping = AsyncMock()
    fake_client.aclose = AsyncMock()
    fake_client.scan_iter = _fake_scan_iter([b"embedv2:m:d512:a", b"embedv2:m:d512:b"])
    fake_client.unlink = AsyncMock(return_value=2)
    with patch(
        "scripts.redis_drop_stale_prefixes.aioredis.from_url",
        return_value=fake_client,
    ):
        rc = drop_mod.main(["embedv2"])
    assert rc == 0
    fake_client.unlink.assert_awaited_once()
