"""Integration-test fixtures.

These tests hit the real Postgres container (``scaffold-postgres``) and the
real schema rather than the dict-mock ``make_mock_db`` used elsewhere in the
suite.

Isolation: each test records the job IDs it touches via the ``track_job`` /
``insert_job`` fixtures. A teardown fixture deletes those rows (and cascades
to ``dag_nodes`` via ``ON DELETE CASCADE``). No savepoint magic — production
code opens its own sessions, so we let it commit and clean up afterward.

asyncpg pool isolation (#flake-2026-05-06): even with the session-scoped
event loop set in pyproject.toml, the engine.dispose() autouse below
catches a residual cross-loop case where SQLAlchemy's pool tries to
``_close_connection`` on a connection whose original asyncio loop has
moved on, raising ``RuntimeError: Event loop is closed`` from
``asyncpg/connection.py:_cancel_current_command``. Disposing the pool
before each test forces a fresh asyncpg connection bound to the current
loop. Cost is one TCP handshake per test (~5 ms against scaffold-postgres);
the integration suite is small (~18 tests) so the overhead is negligible.
"""
from __future__ import annotations

import json

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session, engine




@pytest_asyncio.fixture(autouse=True)
async def _reset_db_pool():
    """Dispose the SQLAlchemy engine's connection pool before every test.

    See module docstring — without this, asyncpg connections from a
    previous test's loop linger in the pool and crash on teardown.
    Disposing yields a fresh pool for the current test's loop.
    """
    await engine.dispose()
    yield


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
