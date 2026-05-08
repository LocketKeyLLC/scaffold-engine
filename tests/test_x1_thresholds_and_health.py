"""Sprint X.1 — Tier 2 audit cleanup, threshold cluster + reranker /health.

Covers:
  - settings defaults match X.1 retunes (orphan 60→30; awaiting_confirmation
    10080→4320)
  - _pre_migration_sweep cutoff INTERVAL '5 minutes' (was '30 minutes')
  - _check_reranker_state shape per app.state contents

Doesn't try to fix the 6 pre-existing failures in tests/test_cleanup.py —
those are stale (test expectations frozen at 6 reaper counts; live code
returns 8). Audit-tail item.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import settings


@pytest.mark.smoke
class TestThresholdDefaults:
    """X.1 settings retunes."""

    def test_node_orphan_threshold_default_is_30(self):
        assert settings.node_orphan_threshold_minutes == 30

    def test_awaiting_confirmation_default_is_72h(self):
        # 72h * 60min = 4320
        assert settings.awaiting_confirmation_stale_minutes == 4320


@pytest.mark.smoke
class TestPreMigrationSweepCutoff:
    """The defensive startup sweep's INTERVAL was tightened from 30 min
    → 5 min in X.1. Inspect the SQL string directly so the test doesn't
    need a live database."""

    def test_sweep_interval_is_5_minutes(self):
        import inspect

        from app import main

        src = inspect.getsource(main._pre_migration_sweep)
        assert "INTERVAL '5 minutes'" in src
        assert "INTERVAL '30 minutes'" not in src


@pytest.mark.smoke
class TestCheckRerankerState:
    """X.1 — _check_reranker_state surfaces lifespan prewarm outcome
    on /health. Six branches: up / down / skipped / unknown / state=None /
    state with no flags."""

    def test_up_when_prewarmed_at_set(self):
        from app.main import _check_reranker_state

        state = SimpleNamespace(
            reranker_prewarmed_at="2026-05-08T03:00:00+00:00",
            reranker_prewarm_elapsed_s=2.5,
            reranker_prewarm_error=None,
            reranker_prewarm_skipped=False,
        )
        result = _check_reranker_state(state)
        assert result["status"] == "up"
        assert result["prewarmed"] is True
        assert result["elapsed_s"] == 2.5
        assert result["prewarmed_at"].startswith("2026-05-08")

    def test_down_when_error_set(self):
        from app.main import _check_reranker_state

        state = SimpleNamespace(
            reranker_prewarmed_at=None,
            reranker_prewarm_elapsed_s=None,
            reranker_prewarm_error="OOM during model load",
            reranker_prewarm_skipped=False,
        )
        result = _check_reranker_state(state)
        assert result["status"] == "down"
        assert result["prewarmed"] is False
        assert "OOM" in result["error"]

    def test_skipped_when_env_disabled_at_boot(self):
        from app.main import _check_reranker_state

        state = SimpleNamespace(
            reranker_prewarmed_at=None,
            reranker_prewarm_elapsed_s=None,
            reranker_prewarm_error=None,
            reranker_prewarm_skipped=True,
        )
        result = _check_reranker_state(state)
        assert result["status"] == "skipped"
        assert result["prewarmed"] is False

    def test_unknown_when_state_is_none(self):
        from app.main import _check_reranker_state

        result = _check_reranker_state(None)
        assert result["status"] == "unknown"
        assert result["prewarmed"] is False

    def test_unknown_when_state_lacks_flags(self):
        """A pre-X.1 build wouldn't set the new attrs at all. _check_reranker_state
        must fall through to 'unknown', not crash."""
        from app.main import _check_reranker_state

        state = SimpleNamespace()  # no attrs
        result = _check_reranker_state(state)
        assert result["status"] == "unknown"
        assert result["prewarmed"] is False

    def test_error_takes_precedence_over_skipped(self):
        """If both error and skipped are somehow set (boot started prewarm
        before SCAFFOLD_PREWARM_RERANKER was honored, etc.), surface the
        error — it's the more actionable signal."""
        from app.main import _check_reranker_state

        state = SimpleNamespace(
            reranker_prewarmed_at=None,
            reranker_prewarm_elapsed_s=None,
            reranker_prewarm_error="loader failure",
            reranker_prewarm_skipped=True,
        )
        result = _check_reranker_state(state)
        assert result["status"] == "down"
        assert "loader failure" in result["error"]
