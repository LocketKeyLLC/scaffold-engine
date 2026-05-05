"""Integration-test fixtures.

These tests hit the real Postgres container (``scaffold-postgres``) and the
real schema rather than the dict-mock ``make_mock_db`` used elsewhere in the
suite.

Isolation: each test records the job IDs it touches via the ``track_job`` /
``insert_job`` fixtures. A teardown fixture deletes those rows (and cascades
to ``dag_nodes`` via ``ON DELETE CASCADE``). No savepoint magic — production
code opens its own sessions, so we let it commit and clean up afterward.
"""
from __future__ import annotations

import json

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session




@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    """A real AsyncSession backed by the app's engine. Test code commits as
    production code would; the ``track_job``/``insert_job`` fixtures handle
    cleanup of any rows the test produced."""
    async with async_session() as session:
        yield session


@pytest_asyncio.fixture
async def tracked_jobs():
    """Collects job IDs produced during a test; teardown deletes them."""
    ids: list[str] = []
    yield ids
    if not ids:
        return
    async with async_session() as session:
        await session.execute(
            text("DELETE FROM jobs WHERE id = ANY(CAST(:ids AS uuid[]))"),
            {"ids": ids},
        )
        await session.commit()


@pytest_asyncio.fixture
async def insert_job(tracked_jobs):
    """Helper: insert a job row in the given status; returns its UUID and
    auto-cleans up after the test finishes."""

    async def _insert(*, status: str = "planning", title: str = "integration test",
                      input_text: str = "test input",
                      refined_brief: dict | None = None) -> str:
        async with async_session() as session:
            row = await session.execute(
                text("""
                    INSERT INTO jobs (title, input_text, status, refined_brief)
                    VALUES (:t, :i, :s, CAST(:b AS JSONB))
                    RETURNING id
                """),
                {
                    "t": title,
                    "i": input_text,
                    "s": status,
                    "b": json.dumps(refined_brief or {"description": "x", "goals": ["g"]}),
                },
            )
            jid = str(row.scalar_one())
            await session.commit()
        tracked_jobs.append(jid)
        return jid

    return _insert


@pytest_asyncio.fixture
async def track_job(tracked_jobs):
    """For tests that call production code which creates a job internally
    (e.g. ``refine_idea``). Pass the resulting job_id to mark it for cleanup."""
    def _track(job_id: str) -> str:
        tracked_jobs.append(job_id)
        return job_id
    return _track
