"""§17.537 + §17.538 — the /assist/_chatmap endpoints.

§17.537: GET augments the recalled entry with the mapped session's live status
so the pipeline can decide whether plain chat routes INTO assist (active) or
falls back to triage (paused/terminal/missing).

§17.538: the chat→session link is also persisted durably on assist_sessions
(PUT) and recovered from Postgres on a Redis miss (GET self-heal), so an
LRU-evicted chatmap key no longer orphans an active session from its chat.

Direct-call tests against the handlers with a fake AsyncSession + mocked recall.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.authz import ADMIN_PRINCIPAL
from app.routers import assist as assist_router


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class _FakeDB:
    """AsyncSession stand-in. `rows` is a list returned per execute() call in
    order (the handler may issue >1 query); a bare value is reused for all.
    Records every (sql_text, params) for assertions and counts commits."""

    def __init__(self, rows=None, raises=False):
        self._rows = rows
        self._raises = raises
        self.executed = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, statement, params=None):
        self.executed.append((str(statement), params))
        if self._raises:
            raise RuntimeError("boom")
        row = self._rows
        if isinstance(self._rows, list):
            row = self._rows[len(self.executed) - 1] if len(self.executed) <= len(self._rows) else None
        return _FakeResult(row)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


# ---------------------------------------------------------------------------
# §17.537 — GET augments Redis hit with live status
# ---------------------------------------------------------------------------


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_active_status_surfaced():
    entry = {"session_id": "s1", "last_node_key": "T1"}
    with patch.object(assist_router.assist_session_map, "recall",
                      AsyncMock(return_value=entry)):
        out = await assist_router.assist_chatmap_get(
            "chat-1", db=_FakeDB(rows=[{"status": "active"}]),
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
        out = await assist_router.assist_chatmap_get("chat-1", db=_FakeDB(rows=[None]))
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


# ---------------------------------------------------------------------------
# §17.538 — PUT persists the link durably
# ---------------------------------------------------------------------------


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_put_persists_chat_id_durably():
    body = assist_router.AssistChatMapInput(session_id="s1", last_node_key="T1")
    db = _FakeDB(rows=[None])
    with patch.object(assist_router.assist_session_map, "remember",
                      AsyncMock()) as remember:
        out = await assist_router.assist_chatmap_put("chat-1", body, db=db, principal=ADMIN_PRINCIPAL)
    assert out == {"chat_id": "chat-1", "stored": True}
    remember.assert_awaited_once()                       # Redis still set
    assert db.commits == 1                                # durable write committed
    sql, params = db.executed[0]
    assert "UPDATE assist_sessions" in sql and "chat_id" in sql
    assert params == {"cid": "chat-1", "sid": "s1"}


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_put_durable_write_failure_is_soft():
    # A DB hiccup must not 500 the PUT — Redis was already set.
    body = assist_router.AssistChatMapInput(session_id="s1", last_node_key=None)
    db = _FakeDB(raises=True)
    with patch.object(assist_router.assist_session_map, "remember", AsyncMock()):
        out = await assist_router.assist_chatmap_put("chat-1", body, db=db, principal=ADMIN_PRINCIPAL)
    assert out == {"chat_id": "chat-1", "stored": True}
    assert db.rollbacks == 1
    assert db.commits == 0


# ---------------------------------------------------------------------------
# §17.538 — GET self-heals from Postgres on a Redis miss
# ---------------------------------------------------------------------------


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_get_recovers_from_pg_and_reseeds_redis():
    # Redis miss → recover the active session bound to this chat from PG.
    db = _FakeDB(rows=[{"id": "sess-uuid", "current_node_key": "T3"}])
    with patch.object(assist_router.assist_session_map, "recall",
                      AsyncMock(return_value=None)), \
         patch.object(assist_router.assist_session_map, "remember",
                      AsyncMock()) as remember:
        out = await assist_router.assist_chatmap_get("chat-1", db=db)
    assert out["session_id"] == "sess-uuid"
    assert out["last_node_key"] == "T3"
    assert out["status"] == "active"
    # recovery query filters to active sessions for this chat
    sql, params = db.executed[0]
    assert "WHERE chat_id = :cid AND status = 'active'" in sql
    assert params == {"cid": "chat-1"}
    # self-heal: Redis re-seeded with the recovered link
    remember.assert_awaited_once()
    _, kw = remember.call_args
    assert kw["session_id"] == "sess-uuid" and kw["last_node_key"] == "T3"


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_get_404_when_no_active_session_for_chat():
    # Redis miss AND no active PG session → 404 (no terminal-session capture).
    db = _FakeDB(rows=[None])
    with patch.object(assist_router.assist_session_map, "recall",
                      AsyncMock(return_value=None)), \
         patch.object(assist_router.assist_session_map, "remember", AsyncMock()):
        with pytest.raises(HTTPException) as ei:
            await assist_router.assist_chatmap_get("chat-1", db=db)
    assert ei.value.status_code == 404


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_get_recovery_db_error_is_404_not_500():
    db = _FakeDB(raises=True)
    with patch.object(assist_router.assist_session_map, "recall",
                      AsyncMock(return_value=None)), \
         patch.object(assist_router.assist_session_map, "remember", AsyncMock()):
        with pytest.raises(HTTPException) as ei:
            await assist_router.assist_chatmap_get("chat-1", db=db)
    assert ei.value.status_code == 404
