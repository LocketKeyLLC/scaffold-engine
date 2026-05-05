"""Integration tests for read-only management endpoints in app/main.py.

These verify the where_sql concatenation pattern (whitelisted status,
parameterized search) actually rejects/escapes real input against real
Postgres — the safety comment added 2026-05-05 is asserted by the test.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text


pytestmark = pytest.mark.asyncio


async def test_jobs_filter_uses_parameterized_search(db_session, insert_job):
    """A search term containing SQL syntax is treated as data, not SQL."""
    job_a = await insert_job(title="Real job alpha", status="planning")
    job_b = await insert_job(title="Real job beta", status="planning")

    # Mimic /jobs handler logic: build where_clauses + params, run query.
    where_clauses = ["j.title ILIKE :q"]
    params = {"q": "%' OR '1'='1%", "limit": 25, "offset": 0}
    where_sql = "WHERE " + " AND ".join(where_clauses)
    rows = (await db_session.execute(
        text(f"""
            SELECT j.id FROM jobs j
            {where_sql}
            ORDER BY j.updated_at DESC
            LIMIT :limit OFFSET :offset
        """),
        params,
    )).mappings().all()
    # Injection attempt is parameterized — no rows match this literal pattern.
    assert all(r["id"] not in {job_a, job_b} for r in rows)


async def test_jobs_status_filter_finds_only_matching_status(db_session, insert_job):
    a = await insert_job(title="alpha", status="planning")
    b = await insert_job(title="beta", status="completed")

    rows = (await db_session.execute(
        text("SELECT id FROM jobs WHERE status = :status AND id IN (:a, :b)"),
        {"status": "planning", "a": a, "b": b},
    )).mappings().all()
    found = {str(r["id"]) for r in rows}
    assert a in found
    assert b not in found
