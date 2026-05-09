"""Audit M4 — PATCH /observability/errors/{id}.

Verifies the resolve endpoint flips the ``resolved`` flag, stamps
``resolved_at`` correctly (NOW on True, NULL on False), persists
the optional resolution note, and rejects bad / missing UUIDs.

Tests at the handler-call level (mocked AsyncSession) rather than via
TestClient — keeps tests fast + DB-independent. The X.29 change is
small and isolated to the new endpoint, so this depth of coverage is
sufficient.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.routers.observability import resolve_error_endpoint
from app.schemas import ErrorLogResolveInput


def _row(*, error_id: str, resolved: bool, resolution: str | None, resolved_at: datetime | None):
    return SimpleNamespace(
        id=uuid.UUID(error_id),
        resolved=resolved,
        resolution=resolution,
        resolved_at=resolved_at,
    )


def _mock_db(returning_row):
    """AsyncSession mock whose execute() returns a result whose fetchone()
    yields ``returning_row`` (or None for the 404 case)."""
    db = AsyncMock()
    result = MagicMock()
    result.fetchone.return_value = returning_row
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    return db


@pytest.mark.smoke
class TestResolveErrorEndpoint:
    async def test_mark_resolved_with_note(self):
        eid = "11111111-2222-3333-4444-555555555555"
        now = datetime(2026, 5, 9, tzinfo=timezone.utc)
        db = _mock_db(_row(
            error_id=eid, resolved=True,
            resolution="fixed_by: W.6 tool_call migration",
            resolved_at=now,
        ))
        body = ErrorLogResolveInput(resolved=True, resolution="fixed_by: W.6 tool_call migration")

        resp = await resolve_error_endpoint(error_id=eid, body=body, db=db)

        assert resp.error_id == eid
        assert resp.resolved is True
        assert resp.resolution == "fixed_by: W.6 tool_call migration"
        assert resp.resolved_at == now.isoformat()
        # Bind params land via the dict, never interpolated.
        params = db.execute.await_args_list[-1].args[1]
        assert params["id"] == eid
        assert params["resolved"] is True
        assert params["resolution"] == "fixed_by: W.6 tool_call migration"
        # CASE expression in the SQL drives resolved_at via the bind.
        sql = db.execute.await_args_list[-1].args[0].text
        assert "CASE WHEN :resolved THEN NOW() ELSE NULL END" in sql
        db.commit.assert_awaited_once()

    async def test_mark_resolved_without_note(self):
        eid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        db = _mock_db(_row(
            error_id=eid, resolved=True, resolution=None,
            resolved_at=datetime.now(timezone.utc),
        ))
        body = ErrorLogResolveInput(resolved=True)  # resolution defaults None

        resp = await resolve_error_endpoint(error_id=eid, body=body, db=db)

        assert resp.resolution is None
        params = db.execute.await_args_list[-1].args[1]
        assert params["resolution"] is None

    async def test_mark_unresolved_clears_resolved_at(self):
        eid = "12345678-1234-1234-1234-123456789012"
        db = _mock_db(_row(
            error_id=eid, resolved=False, resolution=None, resolved_at=None,
        ))
        body = ErrorLogResolveInput(resolved=False, resolution=None)

        resp = await resolve_error_endpoint(error_id=eid, body=body, db=db)

        assert resp.resolved is False
        assert resp.resolved_at is None

    async def test_bad_uuid_returns_422(self):
        db = _mock_db(None)
        body = ErrorLogResolveInput(resolved=True)
        with pytest.raises(HTTPException) as exc:
            await resolve_error_endpoint(error_id="not-a-uuid", body=body, db=db)
        assert exc.value.status_code == 422
        assert "error_id" in exc.value.detail
        # No DB call should fire — UUID parse fails first.
        db.execute.assert_not_called()

    async def test_missing_row_returns_404(self):
        eid = "00000000-0000-0000-0000-000000000000"
        db = _mock_db(None)
        body = ErrorLogResolveInput(resolved=True)

        with pytest.raises(HTTPException) as exc:
            await resolve_error_endpoint(error_id=eid, body=body, db=db)
        assert exc.value.status_code == 404
        assert eid in exc.value.detail
        # The UPDATE did fire (UUID parsed); commit did not.
        db.execute.assert_awaited_once()
        db.commit.assert_not_called()

    async def test_sql_uses_bind_params_not_interpolation(self):
        """SAFE: the SQL string must not contain the user's resolution text."""
        eid = "11111111-1111-1111-1111-111111111111"
        evil = "'); DROP TABLE error_logs; --"
        db = _mock_db(_row(
            error_id=eid, resolved=True, resolution=evil,
            resolved_at=datetime.now(timezone.utc),
        ))
        body = ErrorLogResolveInput(resolved=True, resolution=evil)
        await resolve_error_endpoint(error_id=eid, body=body, db=db)

        sql = db.execute.await_args_list[-1].args[0].text
        # The user-controlled string must not be in the SQL text — it goes
        # through the params dict, where SQLAlchemy bind-quotes it.
        assert evil not in sql
        params = db.execute.await_args_list[-1].args[1]
        assert params["resolution"] == evil
