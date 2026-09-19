"""§17.1118 (Phase 1 ledger U-10) — GET /exec/statuses: several jobs, one round trip."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.routers import workflow as wf


async def test_batch_returns_per_job_payloads_and_lists_the_rest():
    a, b = str(uuid4()), str(uuid4())

    async def fake_status(pid, db):
        return {"job_id": str(pid), "job_status": "running", "progress": {"total": 3, "pct": 33}} if str(pid) == a else {"error": "no execution state"}

    async def fake_visible(db, principal, jid, detail=""):
        if jid == b:
            raise HTTPException(status_code=404, detail=detail)

    with patch.object(wf, "execution_status", fake_status), \
         patch.object(wf, "assert_visible", fake_visible), \
         patch.object(wf.run_broker, "is_running", lambda jid: jid == a):
        out = await wf.exec_statuses(ids=f"{a},{b},not-a-uuid,{a}", db=MagicMock(), principal=MagicMock())
    assert list(out["statuses"]) == [a], "visible + has state → present, de-duplicated"
    assert out["statuses"][a]["detached_running"] is True and out["statuses"][a]["progress"]["pct"] == 33
    assert out["missing"] == [b, "not-a-uuid"]


async def test_batch_rejects_empty_and_oversized():
    with pytest.raises(HTTPException) as e:
        await wf.exec_statuses(ids="", db=MagicMock(), principal=MagicMock())
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        await wf.exec_statuses(ids=",".join(str(uuid4()) for _ in range(51)), db=MagicMock(), principal=MagicMock())
    assert e.value.status_code == 400
