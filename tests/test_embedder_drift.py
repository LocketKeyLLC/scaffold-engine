"""Tests for §17.135 — check_embedder_drift.

Verifies the four normal outcome branches plus fail-soft handling:
  - first_run: empty cache_metadata → insert + no alert
  - unchanged: stored == current → touch updated_at + no alert
  - drift: stored != current → emit cache.embedder_drift + upsert
  - skipped: DB read failure → log + return skipped (no crash)
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.utils import embedder_drift as _drift


def _mock_db_with_lookup(*, stored: str | None):
    """Build an AsyncSession where the SELECT call returns `stored`."""
    lookup_result = MagicMock()
    lookup_result.scalar.return_value = stored
    db = AsyncMock()
    # Default execute mock returns a permissive result for INSERT/UPDATE;
    # the first execute call (SELECT) is overridden by side_effect.
    db.execute = AsyncMock(side_effect=[lookup_result, MagicMock(), MagicMock()])
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


# ---------------------------------------------------------------------------
# first_run
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_first_run_inserts_and_no_alert(monkeypatch):
    monkeypatch.setattr(
        "app.utils.embedder_drift.settings.model_embedder_id",
        "nomic-embed-text-mrl512", raising=False,
    )
    db = _mock_db_with_lookup(stored=None)
    emit_calls: list = []

    async def _spy_emit(**kw):
        emit_calls.append(kw)
        return {"emitted": True}

    with patch("app.observability.alerts.emit", new=_spy_emit):
        out = await _drift.check_embedder_drift(db)

    assert out["outcome"] == "first_run"
    assert out["current"] == "nomic-embed-text-mrl512"
    assert out["stored"] is None
    assert emit_calls == []
    # First call SELECT, second call INSERT-on-conflict
    assert db.execute.await_count == 2
    insert_call = db.execute.await_args_list[1]
    sql_text = str(insert_call.args[0])
    assert "INSERT INTO cache_metadata" in sql_text
    assert insert_call.args[1] == {"k": "active_embedder_id", "v": "nomic-embed-text-mrl512"}
    db.commit.assert_awaited()


# ---------------------------------------------------------------------------
# unchanged
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unchanged_touches_timestamp_no_alert(monkeypatch):
    monkeypatch.setattr(
        "app.utils.embedder_drift.settings.model_embedder_id",
        "nomic-embed-text-mrl512", raising=False,
    )
    db = _mock_db_with_lookup(stored="nomic-embed-text-mrl512")
    emit_calls: list = []

    async def _spy_emit(**kw):
        emit_calls.append(kw)
        return {"emitted": True}

    with patch("app.observability.alerts.emit", new=_spy_emit):
        out = await _drift.check_embedder_drift(db)

    assert out["outcome"] == "unchanged"
    assert out["current"] == out["stored"] == "nomic-embed-text-mrl512"
    assert emit_calls == []
    # SELECT + UPDATE updated_at
    assert db.execute.await_count == 2
    update_call = db.execute.await_args_list[1]
    sql_text = str(update_call.args[0])
    assert "UPDATE cache_metadata SET updated_at" in sql_text


# ---------------------------------------------------------------------------
# drift
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_drift_emits_critical_alert_and_upserts(monkeypatch, caplog):
    monkeypatch.setattr(
        "app.utils.embedder_drift.settings.model_embedder_id",
        "qwen3-embedding:8b-mrl512", raising=False,
    )
    db = _mock_db_with_lookup(stored="nomic-embed-text-mrl512")
    emit_calls: list = []

    async def _spy_emit(**kw):
        emit_calls.append(kw)
        return {"emitted": True}

    with patch("app.observability.alerts.emit", new=_spy_emit):
        with caplog.at_level("CRITICAL", logger="scaffold.embedder_drift"):
            out = await _drift.check_embedder_drift(db)

    assert out["outcome"] == "drift"
    assert out["stored"] == "nomic-embed-text-mrl512"
    assert out["current"] == "qwen3-embedding:8b-mrl512"
    # Exactly one critical alert
    assert len(emit_calls) == 1
    call = emit_calls[0]
    assert call["kind"] == "cache.embedder_drift"
    assert call["severity"] == "critical"
    payload = call["payload"]
    assert payload["stored_embedder_id"] == "nomic-embed-text-mrl512"
    assert payload["configured_embedder_id"] == "qwen3-embedding:8b-mrl512"
    assert "reindex.py" in payload["reindex_command"]
    # Dedup key embeds the value pair so different drifts fire separately
    assert call["dedup_key"] == (
        "cache.embedder_drift:nomic-embed-text-mrl512->qwen3-embedding:8b-mrl512"
    )
    # Logged at CRITICAL severity
    assert any(
        "embedder_drift_detected" in r.getMessage()
        for r in caplog.records
    )
    # Two DB execute calls: the initial SELECT, then the post-alert upsert.
    # (The alert's internal dedup probe doesn't run because emit() is
    # patched out at the seam.)
    assert db.execute.await_count == 2
    upsert_call = db.execute.await_args_list[1]
    assert "INSERT INTO cache_metadata" in str(upsert_call.args[0])
    assert upsert_call.args[1] == {
        "k": "active_embedder_id", "v": "qwen3-embedding:8b-mrl512",
    }


@pytest.mark.asyncio
async def test_drift_upsert_failure_still_returns_drift_outcome(monkeypatch):
    """If the post-alert upsert fails, the function must still report
    outcome=drift so the caller knows the alert fired. The upsert
    failure is recorded as upsert_failed=True for diagnostics."""
    monkeypatch.setattr(
        "app.utils.embedder_drift.settings.model_embedder_id",
        "model_b", raising=False,
    )
    lookup_result = MagicMock()
    lookup_result.scalar.return_value = "model_a"
    db = AsyncMock()
    # emit() is patched out → only two DB execute calls: SELECT + UPSERT.
    # We make the upsert (second call) raise.
    db.execute = AsyncMock(side_effect=[
        lookup_result,                  # SELECT
        RuntimeError("db kaboom"),      # upsert fails
    ])
    db.commit = AsyncMock()

    async def _spy_emit(**kw):
        return {"emitted": True}

    with patch("app.observability.alerts.emit", new=_spy_emit):
        out = await _drift.check_embedder_drift(db)

    assert out["outcome"] == "drift"
    assert out["upsert_failed"] is True


# ---------------------------------------------------------------------------
# skipped — DB error on the initial read
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_db_read_failure_returns_skipped(caplog):
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=ConnectionError("db unreachable"))

    with caplog.at_level("WARNING", logger="scaffold.embedder_drift"):
        out = await _drift.check_embedder_drift(db)

    assert out["outcome"] == "skipped"
    assert out["reason"] == "db_read_failed"
    assert "db unreachable" in out["error"]
    assert any(
        "embedder_drift_check_failed" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_first_run_db_write_failure_returns_skipped(monkeypatch, caplog):
    monkeypatch.setattr(
        "app.utils.embedder_drift.settings.model_embedder_id",
        "nomic-embed-text-mrl512", raising=False,
    )
    lookup_result = MagicMock()
    lookup_result.scalar.return_value = None
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        lookup_result,
        ConnectionError("db went away mid-write"),
    ])
    db.commit = AsyncMock()

    with caplog.at_level("WARNING", logger="scaffold.embedder_drift"):
        out = await _drift.check_embedder_drift(db)

    assert out["outcome"] == "skipped"
    assert out["reason"] == "db_write_failed"
    assert any(
        "embedder_drift_insert_failed" in r.getMessage()
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# Alert robustness — emit failure must NOT mask the drift outcome
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_drift_alert_failure_still_upserts(monkeypatch):
    """If the alert.emit raises, the function logs the failure but
    proceeds to the upsert so subsequent boots see "unchanged" instead
    of re-firing the same alert. This is the path that prevents an
    operator from getting paged on every restart."""
    monkeypatch.setattr(
        "app.utils.embedder_drift.settings.model_embedder_id",
        "model_b", raising=False,
    )
    lookup_result = MagicMock()
    lookup_result.scalar.return_value = "model_a"
    upsert_result = MagicMock()
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[lookup_result, upsert_result])
    db.commit = AsyncMock()

    async def _broken_emit(**kw):
        raise RuntimeError("alert system down")

    with patch("app.observability.alerts.emit", new=_broken_emit):
        out = await _drift.check_embedder_drift(db)

    assert out["outcome"] == "drift"
    # Upsert SQL still executed
    assert db.execute.await_count == 2
    upsert_sql = str(db.execute.await_args_list[1].args[0])
    assert "INSERT INTO cache_metadata" in upsert_sql
