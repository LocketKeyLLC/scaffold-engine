"""§17.467 — completed_at surfaced through the read paths, against real Postgres.

The §17.466 trigger makes jobs.completed_at correct in the DB; §17.467 wires it
into the two surfaces that feed the API + web UI:
  * execution_handler.execution_status() — the /exec/status payload the web
    job-detail page and SDK consume.
  * the GET /jobs list route — its JobSummary item.
"""
from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import text

from app.authz import Principal
from app.modules.execution_handler import execution_status
from app.routers.jobs import list_jobs

pytestmark = pytest.mark.asyncio


async def _complete(db_session, job_id):
    await db_session.execute(
        text("UPDATE jobs SET status = 'completed' WHERE id = :j"),
        {"j": job_id},
    )
    await db_session.commit()


async def test_exec_status_completed_at_none_for_non_terminal(
    db_session, insert_job,
):
    job_id = await insert_job(status="executing")
    payload = await execution_status(UUID(job_id), db_session)
    assert "completed_at" in payload  # key always present
    assert payload["completed_at"] is None


async def test_exec_status_completed_at_iso_for_terminal(db_session, insert_job):
    job_id = await insert_job(status="executing")
    await _complete(db_session, job_id)

    payload = await execution_status(UUID(job_id), db_session)
    assert payload["completed_at"] is not None
    # ISO-8601 string (datetime serialized in the handler, not a raw datetime).
    assert isinstance(payload["completed_at"], str)
    assert "T" in payload["completed_at"]


async def test_jobs_list_item_carries_completed_at(db_session, insert_job):
    title = "completed-at-surface-probe-§17.467"
    job_id = await insert_job(status="executing", title=title)
    await _complete(db_session, job_id)

    resp = await list_jobs(
        q=title, db=db_session, principal=Principal(identity="admin", role="admin")
    )
    item = next((j for j in resp.jobs if j.id == job_id), None)
    assert item is not None, "inserted job not found in list response"
    assert item.completed_at is not None
    assert "T" in item.completed_at
