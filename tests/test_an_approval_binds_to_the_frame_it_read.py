"""§17.1681 — an approval runs the frame the operator READ, never one re-drawn since (live, job c2518102 T2: the run a
re-ask started re-parked T2 with a different draft ten seconds after the operator read the first, and the approval ran
the one they never saw)."""
import asyncio
import pathlib
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.modules import decision_pause as dp
from app.schemas import DecideInput

A = {"commands": ["bash /tmp/in_ct_104.sh start"], "verify": [], "files": [{"path": "/tmp/in_ct_104.sh", "content": "A"}]}


def test_the_id_is_what_would_run():
    assert dp.frame_id_of(A) == dp.frame_id_of(dict(A)) and len(dp.frame_id_of(A)) == 16
    assert dp.frame_id_of({**A, "files": [{"path": "/tmp/in_ct_104.sh", "content": "B"}]}) != dp.frame_id_of(A)
    assert dp.frame_id_of({**A, "commands": ["pct exec 104 -- ls"]}) != dp.frame_id_of(A)
    assert dp.frame_id_of({**A, "verify": ["pct exec 104 -- ls"]}) != dp.frame_id_of(A)
    assert dp.frame_id_of({"question": "Which?", "options": []}) == ""        # a decision node runs nothing


def test_only_a_given_id_that_differs_is_stale():
    assert dp.frame_is_stale("aaa", "bbb")
    assert not dp.frame_is_stale("aaa", "aaa")
    assert not dp.frame_is_stale(None, "bbb") and not dp.frame_is_stale("aaa", "")


def _call(given, current):
    import app.routers.jobs as jr
    resolved = AsyncMock(return_value={"outcome": "ran", "node_status": "done"})
    with patch.object(jr, "assert_visible", AsyncMock()), \
         patch.object(jr.decision_pause, "parked_frame_id", AsyncMock(return_value=current)), \
         patch.object(jr.decision_pause, "resolve_decision", resolved):
        body = DecideInput(node_key="T2", choice="run", frame_id=given)
        try:
            asyncio.run(jr.decide_endpoint("c2518102-c3d1-4742-8b58-2fec219b2ba0", body, db=AsyncMock(),
                                           principal=AsyncMock()))
        except HTTPException as e:
            return e, resolved
        return None, resolved


def test_a_stale_approval_runs_nothing():
    err, resolved = _call("aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb")
    assert err is not None and err.status_code == 409 and err.detail["frame_id"] == "bbbbbbbbbbbbbbbb"
    assert resolved.await_count == 0


def test_a_current_approval_goes_through():
    err, resolved = _call("aaaaaaaaaaaaaaaa", "aaaaaaaaaaaaaaaa")
    assert resolved.await_count == 1


@pytest.mark.asyncio
async def test_the_park_records_the_id():
    db = AsyncMock()
    seen = {}

    async def execute(stmt, params=None):
        if params and "patch" in params:
            seen["patch"] = params["patch"]
    db.execute = execute
    with patch.object(dp, "transition", AsyncMock(return_value=True)):
        await dp.park_awaiting_decision(db, "j", {"node_key": "T2", "title": "t"}, dict(A, kind="run"))
    assert dp.frame_id_of(A) in seen["patch"]


def test_the_spa_sends_the_frame_on_screen():
    spa = (pathlib.Path(dp.__file__).parents[1] / "ui" / "static" / "views" / "theater.js").read_text(encoding="utf-8")
    i = spa.index("function showRunApproval(d)")
    assert "frame_id: d.frame_id" in spa[i:i + 8000]
