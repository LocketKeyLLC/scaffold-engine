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

    async def test_emit_posts_webhook_when_configured(self, monkeypatch):
        """§17.835 (plan 8.7) — URL set → one JSON POST carrying both the
        Slack-compatible `text` line and the full record."""
        monkeypatch.setattr(
            "app.observability.alerts.settings.alert_webhook_url",
            "http://ntfy.local/alerts", raising=False,
        )
        client = MagicMock()
        client.post = AsyncMock(return_value=MagicMock(status_code=200))
        db = _mock_db(insert_id="00000000-0000-0000-0000-000000000002")
        with patch("app.utils.http_clients.get_generic_http_client",
                   return_value=client):
            await _alerts.emit(
                kind="test.hook", severity="critical", message="boom",
                payload={"x": 1}, db=db,
            )
        client.post.assert_awaited_once()
        args, kwargs = client.post.await_args
        assert args[0] == "http://ntfy.local/alerts"
        body = kwargs["json"]
        assert body["text"] == "[critical] test.hook: boom"
        assert body["kind"] == "test.hook" and body["payload"] == {"x": 1}
        assert body["id"] == "00000000-0000-0000-0000-000000000002"

    async def test_emit_webhook_default_off_no_call(self):
        client = MagicMock()
        client.post = AsyncMock()
        with patch("app.utils.http_clients.get_generic_http_client",
                   return_value=client):
            await _alerts.emit(
                kind="test.hook", severity="info", message="quiet",
                db=_mock_db(),
            )
        client.post.assert_not_awaited()

    async def test_emit_webhook_failure_absorbed(self, monkeypatch):
        """A dead receiver can never break alert emission."""
        monkeypatch.setattr(
            "app.observability.alerts.settings.alert_webhook_url",
            "http://dead.local/", raising=False,
        )
        client = MagicMock()
        client.post = AsyncMock(side_effect=RuntimeError("conn refused"))
        with patch("app.utils.http_clients.get_generic_http_client",
                   return_value=client):
            result = await _alerts.emit(
                kind="test.hook", severity="warning", message="still emitted",
                db=_mock_db(),
            )
        assert result["emitted"] is True  # other sinks unaffected

    async def test_emit_webhook_suppressed_alert_not_posted(self, monkeypatch):
        """Cooldown-suppressed alerts don't spam the webhook."""
        monkeypatch.setattr(
            "app.observability.alerts.settings.alert_webhook_url",
            "http://ntfy.local/alerts", raising=False,
        )
        client = MagicMock()
        client.post = AsyncMock()
        with patch("app.utils.http_clients.get_generic_http_client",
                   return_value=client):
            await _alerts.emit(
                kind="test.hook", severity="warning", message="dup",
                dedup_key="k:1", db=_mock_db(dedup_hit=True),
            )
        client.post.assert_not_awaited()

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

    async def test_emit_db_insert_failure_rolls_back(self):
        """§17.598 — a failed INSERT must roll back so the (possibly
        caller-shared) session isn't left in a PendingRollbackError state that
        fail-opens every later read/emit in the same evaluate_thresholds tick."""
        async def _execute(sql, params=None):
            sql_text = str(sql)
            if "FROM system_alerts" in sql_text and "WHERE dedup_key" in sql_text:
                r = MagicMock()
                r.first.return_value = None
                return r
            raise RuntimeError("deadlock detected")

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_execute)
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        result = await _alerts.emit(
            kind="t", severity="info", message="x", db=db,
        )
        assert result["emitted"] is True
        db.rollback.assert_awaited_once()


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


# ---------------------------------------------------------------------------
# §17.388 — per-kind cooldown resolution
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestCooldownResolution:
    """§17.388 — pre-§17.388 every alert kind used the same uniform
    `alert_cooldown_seconds`. The third §17.161 deferred follow-up
    (per-kind cooldown) splits this into a precedence chain:
    emit-time kwarg > per-kind setting > default.
    """

    def test_default_returns_alert_cooldown_seconds(self, monkeypatch):
        monkeypatch.setattr(
            _alerts.settings, "alert_cooldown_seconds", 3600, raising=False,
        )
        monkeypatch.setattr(
            _alerts.settings, "alert_kind_cooldowns", {}, raising=False,
        )
        assert _alerts._resolve_cooldown("any.kind") == 3600

    def test_per_kind_override_beats_default(self, monkeypatch):
        monkeypatch.setattr(
            _alerts.settings, "alert_cooldown_seconds", 3600, raising=False,
        )
        monkeypatch.setattr(
            _alerts.settings, "alert_kind_cooldowns",
            {"host.oom_killed": 300, "calibration.no_fire": 86400},
            raising=False,
        )
        assert _alerts._resolve_cooldown("host.oom_killed") == 300
        assert _alerts._resolve_cooldown("calibration.no_fire") == 86400
        # Other kinds fall back to default.
        assert _alerts._resolve_cooldown("test.unknown") == 3600

    def test_emit_kwarg_overrides_per_kind_and_default(self, monkeypatch):
        """emit-time cooldown_seconds kwarg wins over BOTH per-kind and
        default — the most specific source always wins."""
        monkeypatch.setattr(
            _alerts.settings, "alert_cooldown_seconds", 3600, raising=False,
        )
        monkeypatch.setattr(
            _alerts.settings, "alert_kind_cooldowns",
            {"host.oom_killed": 300}, raising=False,
        )
        # kwarg 60 beats the per-kind 300 and the default 3600.
        assert _alerts._resolve_cooldown("host.oom_killed", 60) == 60
        # kwarg also beats default for a kind with no per-kind entry.
        assert _alerts._resolve_cooldown("test.kind", 5) == 5

    def test_kwarg_zero_disables_dedup_for_that_call(self, monkeypatch):
        """cooldown=0 means 'every emit lands a row' — useful for test
        seeding or one-off burst-mode emits."""
        monkeypatch.setattr(
            _alerts.settings, "alert_cooldown_seconds", 3600, raising=False,
        )
        assert _alerts._resolve_cooldown("any.kind", 0) == 0

    def test_negative_override_clamps_to_zero(self, monkeypatch):
        """Defensive: a caller passing a negative cooldown gets 0
        (disable dedup) rather than negative-interval SQL errors."""
        monkeypatch.setattr(
            _alerts.settings, "alert_cooldown_seconds", 3600, raising=False,
        )
        assert _alerts._resolve_cooldown("any.kind", -100) == 0

    async def test_emit_uses_per_kind_cooldown_in_dedup_probe(self, monkeypatch):
        """End-to-end: when a per-kind cooldown is configured, emit()
        passes it to the dedup probe (not the default)."""
        monkeypatch.setattr(
            _alerts.settings, "alert_cooldown_seconds", 3600, raising=False,
        )
        monkeypatch.setattr(
            _alerts.settings, "alert_kind_cooldowns",
            {"test.kind": 60}, raising=False,
        )
        captured_window: list[int] = []

        async def fake_execute(sql, params=None):
            sql_text = str(sql)
            result = MagicMock()
            if "FROM system_alerts" in sql_text and "WHERE dedup_key" in sql_text:
                # Capture the `:w` (cooldown) param passed to the probe.
                captured_window.append(int(params.get("w", -1)))
                result.first.return_value = None  # no cooldown match
                return result
            if "INSERT INTO system_alerts" in sql_text:
                result.scalar.return_value = str(uuid4())
                return result
            return result

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=fake_execute)
        db.commit = AsyncMock()
        await _alerts.emit(
            kind="test.kind", severity="warning", message="x",
            dedup_key="test:1", db=db,
        )
        assert captured_window == [60], (
            "dedup probe should receive the per-kind 60s, not the default 3600s"
        )

    async def test_emit_kwarg_beats_per_kind_in_dedup_probe(self, monkeypatch):
        """End-to-end: emit-time kwarg propagates to the dedup probe."""
        monkeypatch.setattr(
            _alerts.settings, "alert_cooldown_seconds", 3600, raising=False,
        )
        monkeypatch.setattr(
            _alerts.settings, "alert_kind_cooldowns",
            {"test.kind": 60}, raising=False,
        )
        captured_window: list[int] = []

        async def fake_execute(sql, params=None):
            sql_text = str(sql)
            result = MagicMock()
            if "FROM system_alerts" in sql_text and "WHERE dedup_key" in sql_text:
                captured_window.append(int(params.get("w", -1)))
                result.first.return_value = None
                return result
            if "INSERT INTO system_alerts" in sql_text:
                result.scalar.return_value = str(uuid4())
                return result
            return result

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=fake_execute)
        db.commit = AsyncMock()
        await _alerts.emit(
            kind="test.kind", severity="warning", message="x",
            dedup_key="test:1", db=db, cooldown_seconds=15,
        )
        assert captured_window == [15], (
            "emit-time kwarg should beat both per-kind 60s and default 3600s"
        )

    def test_cli_propagates_cooldown_seconds_flag(self):
        emit_call = AsyncMock(return_value={"emitted": True, "suppressed": False, "id": "x", "reason": None})
        with patch("app.observability.alerts.emit", new=emit_call):
            rc = _alerts.main([
                "emit", "--kind", "host.oom_killed",
                "--message", "x", "--cooldown-seconds", "120",
            ])
        assert rc == 0
        assert emit_call.await_args.kwargs["cooldown_seconds"] == 120

    def test_cli_default_passes_none_cooldown(self):
        """Without the flag, the CLI passes cooldown_seconds=None so
        emit() falls into the per-kind / default resolution chain."""
        emit_call = AsyncMock(return_value={"emitted": True, "suppressed": False, "id": "x", "reason": None})
        with patch("app.observability.alerts.emit", new=emit_call):
            rc = _alerts.main([
                "emit", "--kind", "host.oom_killed", "--message", "x",
            ])
        assert rc == 0
        assert emit_call.await_args.kwargs["cooldown_seconds"] is None


# ---------------------------------------------------------------------------
# §17.388 — Pydantic validator on alert_kind_cooldowns
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestAlertKindCooldownsValidator:
    """§17.388 — the model_validator on Settings clamps each dict value
    to [0, 86400] (same range as alert_cooldown_seconds Field). Bad
    values are clamped + logged, NOT rejected — a typo in env shouldn't
    crash the orchestrator at boot.
    """

    def test_valid_values_pass_through(self):
        from app.config import Settings
        s = Settings(alert_kind_cooldowns={"a.b": 300, "c.d": 86400})
        assert s.alert_kind_cooldowns == {"a.b": 300, "c.d": 86400}

    def test_negative_clamps_to_zero(self):
        from app.config import Settings
        s = Settings(alert_kind_cooldowns={"a.b": -50})
        assert s.alert_kind_cooldowns == {"a.b": 0}

    def test_over_cap_clamps_to_86400(self):
        from app.config import Settings
        s = Settings(alert_kind_cooldowns={"a.b": 999999})
        assert s.alert_kind_cooldowns == {"a.b": 86400}

    def test_non_int_value_is_dropped(self):
        """A non-int value (e.g. dict-typing-bypass via JSON env) is
        dropped from the resolved dict so callers can't get e.g. a list
        passed to make_interval."""
        from app.config import Settings
        # Pydantic v2 coerces strings to int when possible; explicit
        # non-coercible types like list/dict should be dropped. To
        # bypass Pydantic's coercion entirely, construct then mutate.
        s = Settings()
        object.__setattr__(s, "alert_kind_cooldowns", {"a.b": "not-an-int"})
        # Re-run the validator manually.
        s = s._validate_alert_kind_cooldowns()
        assert "a.b" not in s.alert_kind_cooldowns

    def test_empty_dict_is_the_default(self):
        from app.config import Settings
        s = Settings()
        assert s.alert_kind_cooldowns == {}
