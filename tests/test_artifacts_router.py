"""Tests for the artifacts read endpoints (§17.565).

Calls the router coroutines directly with an AsyncMock DB session, mirroring
the unit-level style used elsewhere for routers.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.routers.artifacts import list_job_artifacts, get_artifact


def _row(**kw):
    base = {
        "id": uuid4(),
        "job_id": uuid4(),
        "node_id": None,
        "artifact_type": "report",
        "title": "Deliverable",
        "content": "# body",
        "file_path": None,
        "mime_type": "text/markdown",
        "size_bytes": 6,
        "metadata": {},
        "created_at": datetime(2026, 6, 20, tzinfo=timezone.utc),
    }
    base.update(kw)
    return base


def _result(all_rows=None, first_row=None):
    r = MagicMock()
    m = MagicMock()
    m.all.return_value = all_rows or []
    m.first.return_value = first_row
    r.mappings.return_value = m
    return r


def _db(result):
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    return db


_JID = "11111111-1111-4111-8111-111111111111"
_AID = "22222222-2222-4222-8222-222222222222"


class TestListJobArtifacts:
    @pytest.mark.asyncio
    async def test_returns_artifacts(self):
        rows = [_row(artifact_type="report"), _row(artifact_type="code",
                                                   node_id=uuid4())]
        resp = await list_job_artifacts(_JID, db=_db(_result(all_rows=rows)))
        assert resp.total == 2
        assert {a.artifact_type for a in resp.artifacts} == {"report", "code"}

    @pytest.mark.asyncio
    async def test_empty(self):
        resp = await list_job_artifacts(_JID, db=_db(_result(all_rows=[])))
        assert resp.total == 0
        assert resp.artifacts == []

    @pytest.mark.asyncio
    async def test_bad_uuid_422(self):
        with pytest.raises(HTTPException) as exc:
            await list_job_artifacts("not-a-uuid", db=_db(_result()))
        assert exc.value.status_code == 422


class TestGetArtifact:
    @pytest.mark.asyncio
    async def test_returns_one(self):
        resp = await get_artifact(_AID, db=_db(_result(first_row=_row())))
        assert resp.title == "Deliverable"
        assert resp.content == "# body"

    @pytest.mark.asyncio
    async def test_404_when_missing(self):
        with pytest.raises(HTTPException) as exc:
            await get_artifact(_AID, db=_db(_result(first_row=None)))
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_bad_uuid_422(self):
        with pytest.raises(HTTPException) as exc:
            await get_artifact("nope", db=_db(_result()))
        assert exc.value.status_code == 422
