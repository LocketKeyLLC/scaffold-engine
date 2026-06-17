"""§17.537 — the /assist/_chatmap GET now augments the recalled entry with the
mapped session's live status, so the pipeline can decide whether plain chat
routes INTO assist (active) or falls back to triage (paused/terminal/missing).

Direct-call tests against the handler with a fake AsyncSession + mocked recall.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.routers import assist as assist_router


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class _FakeDB:
    """Minimal AsyncSession stand-in: execute() → mappings().first()."""

    def __init__(self, row=None, raises=False):
        self._row = row
        self._raises = raises

    async def execute(self, *args, **kwargs):
        if self._raises:
            raise RuntimeError("boom")
        return _FakeResult(self._row)


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_active_status_surfaced():
    entry = {"session_id": "s1", "last_node_key": "T1"}
    with patch.object(assist_router.assist_session_map, "recall",
                      AsyncMock(return_value=entry)):
        out = await assist_router.assist_chatmap_get(
            "chat-1", db=_FakeDB(row={"status": "active"}),
        )
    assert out["session_id"] == "s1"
    assert out["last_node_key"] == "T1"
    assert out["status"] == "active"


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_missing_session_row_yields_null_status():
    entry = {"session_id": "ghost", "last_node_key": None}
    with patch.object(assist_router.assist_session_map, "recall",
                      AsyncMock(return_value=entry)):
        out = await assist_router.assist_chatmap_get("chat-1", db=_FakeDB(row=None))
    assert out["status"] is None


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_db_error_degrades_to_null_status_not_500():
    entry = {"session_id": "s1", "last_node_key": "T1"}
    with patch.object(assist_router.assist_session_map, "recall",
                      AsyncMock(return_value=entry)):
        out = await assist_router.assist_chatmap_get(
            "chat-1", db=_FakeDB(raises=True),
        )
    assert out["status"] is None
    assert out["session_id"] == "s1"


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_no_map_still_404s():
    with patch.object(assist_router.assist_session_map, "recall",
                      AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as ei:
            await assist_router.assist_chatmap_get("chat-1", db=_FakeDB())
    assert ei.value.status_code == 404
