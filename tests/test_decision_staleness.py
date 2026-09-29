"""§17.1200 — a decision is consumed by the attempt it authorised.

`_hand_back_for_approval` skips the operator's approval when the job already
records a decision for that node. That record exists to stop ONE pending node
being asked twice in a single pass. It must not silence the ask for a node that
has since RUN and been put back.

Live, and this is the worst kind of bug the arc can produce: ADD65 was
approved, its supervised run failed on a privilege error, the node was reopened
to retry — and the stale decision sent it down the ordinary path, where a
hands-on step is written up as a runbook and marked `done`. A step that changes
a machine was recorded as finished having executed nothing, which is precisely
what §17.1183–1186 exist to prevent.

The test for it is a comparison of two timestamps, because that is all the
distinction is: the moment the operator answered, against the moment the node
was last written.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.modules import execution_agent as ea

pytestmark = pytest.mark.asyncio


def _db(updated_at, status="pending"):
    db = MagicMock()
    res = MagicMock()
    res.mappings.return_value.first.return_value = (
        None if updated_at is None else {"updated_at": updated_at, "status": status})
    db.execute = AsyncMock(return_value=res)
    return db


ANSWERED = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)
ENTRY = {"by": "operator", "choice": "run", "at": ANSWERED.isoformat()}


async def test_a_decision_answered_after_the_node_was_last_written_still_stands():
    """The delegate case: the answer is recorded and the node is left pending
    for the next pass to execute. Nothing has touched it since, so the approval
    is genuinely already given and must not be asked again."""
    db = _db(ANSWERED - timedelta(minutes=5))
    assert await ea._decision_is_stale(db, "j", "ADD65", ENTRY) is False


async def test_a_decision_is_stale_once_the_node_has_been_written_since():
    """The ADD65 case: approved, ran, failed, reopened. The node's own
    `updated_at` moved past the answer, so the answer belongs to that attempt."""
    db = _db(ANSWERED + timedelta(seconds=30))
    assert await ea._decision_is_stale(db, "j", "ADD65", ENTRY) is True


@pytest.mark.parametrize("entry", [{}, {"by": "operator"}, {"at": ""}, {"at": "not a date"}, {"at": None}])
async def test_an_unreadable_record_counts_as_stale(entry):
    """Asking once more costs a click. The other way round marks a
    machine-changing step done without running it."""
    db = _db(ANSWERED)
    assert await ea._decision_is_stale(db, "j", "ADD65", entry) is True


async def test_a_missing_node_counts_as_stale():
    assert await ea._decision_is_stale(_db(None), "j", "GONE", ENTRY) is True


async def test_a_naive_timestamp_counts_as_stale():
    """A record written without a timezone cannot be compared to a TIMESTAMPTZ,
    and guessing which way is the failure that put a runbook in a done step."""
    db = _db(ANSWERED + timedelta(minutes=5))
    assert await ea._decision_is_stale(db, "j", "ADD65", {"at": "2026-09-28T12:00:00"}) is True


def test_the_seam_consults_staleness_before_skipping_the_approval():
    import inspect
    src = inspect.getsource(ea._hand_back_for_approval)
    assert "await _decision_is_stale(db, job_id, node_key, entry)" in src
    assert "if node_key in decided:" not in src, "the unconditional skip is what marked a step done unrun"
