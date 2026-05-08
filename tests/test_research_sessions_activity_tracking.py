"""Sprint X.5 — research_sessions.last_activity_at tracking.

Verifies:
  - Cleanup reaper SQL keys on `last_activity_at`, not `updated_at`
  - The 3 "real activity" UPDATE sites in research_state.py write
    `last_activity_at = NOW()` alongside `updated_at = NOW()`
  - Metadata-only / lifecycle-terminal sites do NOT touch `last_activity_at`
    (regression guard so the reaper signal stays meaningful)

Why this matters: migration 021 wired an auto-update trigger on
`updated_at`, so without a separate signal the reaper can't distinguish
a genuinely-idle session from one that was just renamed or touched by
the pre-migration sweep. The fix mirrors the assist_sessions pattern.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import cleanup
from app.modules import research_state as rs


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _async_cm(db):
    class _CM:
        async def __aenter__(self): return db
        async def __aexit__(self, *a): return False
    return _CM()


@pytest.mark.smoke
class TestReaperUsesLastActivityAt:
    """The research-sessions reaper must key on last_activity_at."""

    def test_reap_research_sessions_sql_uses_last_activity_at(self):
        sql = cleanup._REAP_RESEARCH_SESSIONS_SQL
        assert "last_activity_at <" in sql, (
            "reaper must compare last_activity_at to the threshold; "
            "if you switched it back to updated_at, see X.5 — that's the bug "
            "that lets metadata touches mask idle sessions"
        )
        # Belt-and-suspenders: the old comparison must be gone from the
        # WHERE clause so a future merge doesn't accidentally restore it.
        assert "updated_at < NOW() - make_interval" not in sql

    def test_paused_research_reaper_unchanged(self):
        """The paused-research reaper uses pause_expires_at (not
        last_activity_at) — that's a TTL, not an idleness signal. Regression
        guard: X.5 must NOT have changed this SQL."""
        sql = cleanup._REAP_PAUSED_RESEARCH_SQL
        assert "pause_expires_at < NOW()" in sql
        assert "last_activity_at" not in sql


@pytest.mark.smoke
class TestActivitySitesWriteLastActivityAt:
    """The 3 real-activity UPDATE sites must set last_activity_at = NOW()."""

    def test_update_session_iteration_writes_last_activity_at(self):
        state = rs.ResearchState(topic="t", depth="shallow", domain="eng")
        state.iteration = 2
        fake_db = MagicMock()
        fake_db.execute = AsyncMock()
        fake_db.commit = AsyncMock()

        with patch.object(rs, "_ra") as mock_ra:
            mock_ra.return_value.async_session = lambda: _async_cm(fake_db)
            _run(rs._update_session_iteration("sid", state, coverage=0.5))

        fake_db.execute.assert_awaited_once()
        sql_text = fake_db.execute.call_args.args[0].text
        assert "last_activity_at = NOW()" in sql_text
        # Must not have replaced updated_at — both bumps coexist.
        assert "updated_at = NOW()" in sql_text

    def test_pause_session_writes_last_activity_at(self):
        state = rs.ResearchState(topic="t", depth="shallow", domain="eng")
        fake_db = MagicMock()
        fake_db.execute = AsyncMock()
        fake_db.commit = AsyncMock()

        with patch.object(rs, "_ra") as mock_ra:
            mock_ra.return_value.async_session = lambda: _async_cm(fake_db)
            _run(rs._pause_session("sid", state, "Q?", ttl_seconds=600))

        fake_db.execute.assert_awaited_once()
        sql_text = fake_db.execute.call_args.args[0].text
        assert "last_activity_at = NOW()" in sql_text
        assert "updated_at = NOW()" in sql_text

    def test_atomic_claim_for_resume_writes_last_activity_at(self):
        fake_db = MagicMock()
        result = MagicMock()
        result.rowcount = 1
        fake_db.execute = AsyncMock(return_value=result)
        fake_db.commit = AsyncMock()

        with patch.object(rs, "_ra") as mock_ra:
            mock_ra.return_value.async_session = lambda: _async_cm(fake_db)
            _run(rs._atomic_claim_for_resume("sid", "reply text"))

        fake_db.execute.assert_awaited_once()
        sql_text = fake_db.execute.call_args.args[0].text
        assert "last_activity_at = NOW()" in sql_text
        assert "updated_at = NOW()" in sql_text


@pytest.mark.smoke
class TestNonActivitySitesDoNotTouchLastActivityAt:
    """Metadata-only / terminal sites must NOT touch last_activity_at —
    otherwise the reaper signal becomes indistinguishable from updated_at."""

    def test_finalize_session_does_not_write_last_activity_at(self):
        """Terminal status transitions are out of reaper scope (the row
        leaves the WHERE-clause set when status moves out of pending/running),
        so `last_activity_at` need not be bumped. Negative regression
        guard."""
        fake_db = MagicMock()
        fake_db.execute = AsyncMock()
        fake_db.commit = AsyncMock()

        with patch.object(rs, "_ra") as mock_ra:
            mock_ra.return_value.async_session = lambda: _async_cm(fake_db)
            _run(rs._finalize_session(
                "sid", "completed", duration_ms=1234,
                summary="done", error_message=None,
            ))

        fake_db.execute.assert_awaited_once()
        sql_text = fake_db.execute.call_args.args[0].text
        # Terminal write only bumps updated_at — last_activity_at is not
        # part of the SET clause. (If a future change adds it here it's
        # harmless but defeats the negative-test signal; update this
        # test deliberately if scope changes.)
        assert "last_activity_at" not in sql_text
        assert "updated_at = NOW()" in sql_text
