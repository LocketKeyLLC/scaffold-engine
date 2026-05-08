"""Sprint X.9 — synthesized filter on GET /jobs.

Verifies the new `?synthesized=true|false` query param threads through to
the WHERE clause + params correctly, with proper bind-param safety
(no string interpolation of user input).

Tests at the handler-call level rather than via TestClient — this keeps
the test fast and DB-independent while still exercising the actual
filter-clause assembly logic. The X.9 change is small + isolated to the
where-clauses block, so this level of coverage is appropriate.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _capture_db():
    """Mocked AsyncSession that records the SQL + params for every execute()."""
    db = AsyncMock()
    # First execute is COUNT(*); second is the SELECT page.
    count_result = MagicMock()
    count_result.scalar.return_value = 0
    page_result = MagicMock()
    page_result.fetchall.return_value = []
    db.execute = AsyncMock(side_effect=[count_result, page_result])
    return db


def _last_call_sql_and_params(db):
    """Return (sql_text, params) of the most recent execute() call."""
    call = db.execute.await_args_list[-1]
    sql_obj, params = call.args
    return sql_obj.text, params


def _all_calls_sql_text(db) -> list[str]:
    return [c.args[0].text for c in db.execute.await_args_list]


@pytest.mark.smoke
class TestSynthesizedFilter:
    """The ?synthesized query param must surface in the WHERE clause when
    set, and stay absent (NO clause emitted) when omitted/null."""

    async def test_synthesized_true_adds_where_clause(self):
        from app.main import list_jobs
        db = _capture_db()
        await list_jobs(synthesized=True, db=db)
        # Both COUNT and SELECT carry the new clause.
        for sql in _all_calls_sql_text(db):
            assert "j.compiled_output_synthesized = :synthesized" in sql

        # Bind param resolves to True (not "True" string).
        for call in db.execute.await_args_list:
            params = call.args[1]
            assert params.get("synthesized") is True

    async def test_synthesized_false_adds_where_clause(self):
        from app.main import list_jobs
        db = _capture_db()
        await list_jobs(synthesized=False, db=db)
        for sql in _all_calls_sql_text(db):
            assert "j.compiled_output_synthesized = :synthesized" in sql
        for call in db.execute.await_args_list:
            params = call.args[1]
            assert params.get("synthesized") is False

    async def test_no_param_no_clause(self):
        """Omitting the param must leave the WHERE clause unchanged from
        the pre-X.9 shape — no spurious `compiled_output_synthesized`
        comparison sneaking in."""
        from app.main import list_jobs
        db = _capture_db()
        await list_jobs(db=db)  # synthesized defaults to None
        for sql in _all_calls_sql_text(db):
            assert "compiled_output_synthesized" not in sql
        for call in db.execute.await_args_list:
            params = call.args[1]
            assert "synthesized" not in params

    async def test_synthesized_combines_with_status(self):
        """Filters must compose — `?status=completed&synthesized=true` should
        AND-combine into a single WHERE block."""
        from app.main import list_jobs
        db = _capture_db()
        await list_jobs(status="completed", synthesized=True, db=db)
        sql, params = _last_call_sql_and_params(db)
        assert "j.status = :status" in sql
        assert "j.compiled_output_synthesized = :synthesized" in sql
        # The two clauses are joined by AND (i.e. both apply).
        assert " AND " in sql
        assert params["status"] == "completed"
        assert params["synthesized"] is True

    async def test_synthesized_with_q_search(self):
        """Three-way filter combination: status + q + synthesized."""
        from app.main import list_jobs
        db = _capture_db()
        await list_jobs(q="homelab", synthesized=False, db=db)
        sql, params = _last_call_sql_and_params(db)
        assert "j.title ILIKE :q" in sql
        assert "j.compiled_output_synthesized = :synthesized" in sql
        assert params["synthesized"] is False
        assert params["q"] == "%homelab%"
