"""Phase 2 client-disconnect handling — research_and_compile must mark
a job as 'cancelled' on asyncio.CancelledError instead of leaving it
stranded in 'researching'/'planning'.
"""
from __future__ import annotations
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import ideation_workflow


@pytest.mark.asyncio
async def test_phase2_cancelled_marks_job_cancelled():
    """CancelledError mid-Phase-2 -> _cancel_job called, exception re-raised."""
    job_id = "00000000-0000-0000-0000-000000000001"

    # Atomic claim succeeds
    claim_result = MagicMock()
    claim_result.mappings.return_value.first.return_value = {
        "research_data": None,
        "refined_brief": {"title": "x", "domain": "eng"},
    }
    db = MagicMock()
    db.execute = AsyncMock(return_value=claim_result)
    db.commit = AsyncMock()
    db.close = AsyncMock()

    # Force CancelledError once we're past the claim, inside the try block
    async def boom(*a, **kw):
        raise asyncio.CancelledError()

    cancel_db_mock = MagicMock()
    cancel_db_mock.execute = AsyncMock()
    cancel_db_mock.commit = AsyncMock()

    class _SessionCtx:
        async def __aenter__(self_inner):
            return cancel_db_mock
        async def __aexit__(self_inner, *a):
            return False

    with patch.object(ideation_workflow, "search_searxng", side_effect=boom), \
         patch.object(ideation_workflow, "async_session", return_value=_SessionCtx()):
        with pytest.raises(asyncio.CancelledError):
            await ideation_workflow.research_and_compile(job_id, db)

    # _cancel_job ran exactly one UPDATE with the disconnect reason
    assert cancel_db_mock.execute.await_count == 1
    assert cancel_db_mock.commit.await_count == 1
    params = cancel_db_mock.execute.await_args.args[1]
    assert params == {"err": "client_disconnect", "id": job_id}
