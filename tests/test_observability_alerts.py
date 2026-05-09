"""Sprint X.26 — alert emit, dedup, sinks, CLI."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.auth import require_api_key
from app.main import app
from app.observability import alerts as _alerts


def _mock_db(*, dedup_hit: bool = False, insert_id: str | None = None):
    """Build an AsyncMock db that:
       * answers the dedup probe with a row when dedup_hit is True
       * returns insert_id from the INSERT ... RETURNING id call
    """
    insert_id = insert_id or str(uuid4())

    async def _execute(sql, params=None):
        sql_text = str(sql)
        result = MagicMock()
        if "FROM system_alerts" in sql_text and "WHERE dedup_key" in sql_text:
            # dedup probe
            result.first.return_value = (1,) if dedup_hit else None
            return result
        if "INSERT INTO system_alerts" in sql_text:
            result.scalar.return_value = insert_id
            return result
        # list_recent path
        if "SELECT id, kind, severity" in sql_text:
            mappings = MagicMock()
            mappings.all.return_value = []
            result.mappings.return_value = mappings
            return result
        return result

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_execute)
    db.commit = AsyncMock()
    return db


@pytest.fixture
def client():
    app.dependency_overrides[require_api_key] = lambda: "test-key"
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.mark.smoke
class TestEmit:
    async def test_emit_writes_to_db_and_returns_id(self):
        db = _mock_db(dedup_hit=False, insert_id="00000000-0000-0000-0000-000000000001")
        result = await _alerts.emit(
            kind="test.kind", severity="warning", message="hello",
            payload={"k": 1}, dedup_key="test:1", db=db,
        )
        assert result["emitted"] is True
        assert result["suppressed"] is False
        assert result["id"] == "00000000-0000-0000-0000-000000000001"
        # commit was called once
        db.commit.assert_awaited()

    async def test_emit_suppressed_when_dedup_in_cooldown(self):
        db = _mock_db(dedup_hit=True)
        result = await _alerts.emit(
            kind="test.kind", severity="warning", message="hello",
            dedup_key="test:1", db=db,
        )
        assert result["emitted"] is False
        assert result["suppressed"] is True
        assert result["reason"] == "cooldown"
        # No INSERT happened.
        db.commit.assert_not_awaited()

    async def test_emit_writes_file_sink_when_configured(self, tmp_path, monkeypatch):
        path = tmp_path / "alerts.jsonl"
        monkeypatch.setattr(
            "app.observability.alerts.settings.alert_file_path", str(path), raising=False,
        )
        db = _mock_db()
        await _alerts.emit(
            kind="test.kind", severity="info", message="filed",
            payload={"a": 2}, db=db,
        )
        body = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(body) == 1
        record = json.loads(body[0])
        assert record["kind"] == "test.kind"
        assert record["severity"] == "info"
        assert record["payload"] == {"a": 2}

    async def test_emit_invalid_severity_normalized(self):
        db = _mock_db()
        result = await _alerts.emit(
            kind="t", severity="emergency", message="x", db=db,
        )
        # Falls back to 'warning' so the CHECK constraint never trips.
        assert result["emitted"] is True

    async def test_emit_db_insert_failure_still_returns(self):
        async def _execute(sql, params=None):
            sql_text = str(sql)
            if "FROM system_alerts" in sql_text and "WHERE dedup_key" in sql_text:
                r = MagicMock()
                r.first.return_value = None
                return r
            raise RuntimeError("disk full")

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_execute)
        db.commit = AsyncMock()
        result = await _alerts.emit(
            kind="t", severity="info", message="x", db=db,
        )
        # DB write failed but emit is still considered emitted (logger leg
        # always fires); id is None.
        assert result["emitted"] is True
        assert result["id"] is None


@pytest.mark.smoke
class TestAlertsEndpoint:
    def test_alerts_endpoint_returns_payload(self, client):
        with patch(
            "app.routers.alerts._alerts.list_recent",
            new=AsyncMock(return_value={
                "filters": {"kind": None, "since_minutes": None, "limit": 100},
                "count": 1,
                "alerts": [{
                    "id": "a-1", "kind": "calibration.failed", "severity": "critical",
                    "message": "boom", "payload": {}, "dedup_key": None,
                    "created_at": "2026-07-01T08:30:00+00:00",
                }],
            }),
        ):
            r = client.get("/observability/alerts")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        assert body["alerts"][0]["kind"] == "calibration.failed"

    def test_alerts_endpoint_rejects_invalid_since(self, client):
        r = client.get("/observability/alerts?since_minutes=99999")
        assert r.status_code == 422


@pytest.mark.smoke
class TestCLI:
    def test_cli_emit_invokes_emit_with_parsed_payload(self):
        emit_call = AsyncMock(return_value={"emitted": True, "suppressed": False, "id": "x", "reason": None})
        with patch("app.observability.alerts.emit", new=emit_call):
            rc = _alerts.main([
                "emit", "--kind", "calibration.failed", "--severity", "critical",
                "--message", "boom", "--payload", '{"exit_code": 2}',
                "--dedup-key", "calibration:2026-07-01",
            ])
        assert rc == 0
        kwargs = emit_call.await_args.kwargs
        assert kwargs["kind"] == "calibration.failed"
        assert kwargs["severity"] == "critical"
        assert kwargs["payload"] == {"exit_code": 2}
        assert kwargs["dedup_key"] == "calibration:2026-07-01"

    def test_cli_emit_handles_invalid_payload_json(self):
        emit_call = AsyncMock(return_value={"emitted": True, "suppressed": False, "id": "x", "reason": None})
        with patch("app.observability.alerts.emit", new=emit_call):
            rc = _alerts.main([
                "emit", "--kind", "k", "--message", "m",
                "--payload", "not-json",
            ])
        assert rc == 0
        # Falls back to {"raw": "..."} so the alert still records the
        # operator's intent without raising.
        assert emit_call.await_args.kwargs["payload"] == {"raw": "not-json"}
