# tests/test_paper_enforce_read_only_data.py
"""Tests for the read-only enforcement on the dashboard's data path.

The SQL tests run against the real scratch database: every test starts from an
empty ``pnl`` table (the append-only tables refuse DELETE, so nothing is ever
removed here) and inserts only the rows it needs -- each test uses its own
``as_of_ts`` so tests cannot see each other's rows.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.database import async_session
from app.modules.paper_enforce_read_only_data import (
    ALLOWED_LEADING_KEYWORDS,
    FORBIDDEN_KEYWORDS,
    READ_ONLY_TRANSACTION_SQL,
    ReadOnlyViolation,
    assert_read_only_sql,
    is_read_only_sql,
    is_read_only_violation,
    read_only_session,
)

BASE_TS = datetime(2026, 11, 22, 12, 0)


def _ts(minutes: int) -> datetime:
    return BASE_TS + timedelta(minutes=minutes)


async def _insert_pnl(ts: datetime, account_value: float = 1000.0, cumulative_return: float = 0.0) -> None:
    """Insert a pnl row with every NOT NULL column supplied."""
    async with async_session() as db:
        await db.execute(
            text(
                "INSERT INTO pnl (as_of_ts, account_value, cumulative_return, cash, equity) "
                "VALUES (:ts, :av, :cr, :cash, :equity)"
            ),
            {"ts": ts, "av": account_value, "cr": cumulative_return, "cash": 0.0, "equity": account_value},
        )
        await db.commit()


async def _pnl_count(ts: datetime) -> int:
    async with async_session() as db:
        result = await db.execute(
            text("SELECT count(*) FROM pnl WHERE as_of_ts = :ts"), {"ts": ts}
        )
        return int(result.scalar())


# --------------------------------------------------------------------------
# the rules
# --------------------------------------------------------------------------


def test_the_read_only_statement_is_the_transaction_level_one():
    assert READ_ONLY_TRANSACTION_SQL == "SET TRANSACTION READ ONLY"
    assert "READ ONLY" in READ_ONLY_TRANSACTION_SQL.upper()


def test_the_guard_allows_only_read_shaped_leading_keywords():
    assert ALLOWED_LEADING_KEYWORDS == {"select", "with", "show", "explain", "table", "values"}


@pytest.mark.parametrize("word", sorted(FORBIDDEN_KEYWORDS))
def test_every_forbidden_keyword_is_refused(word):
    with pytest.raises(ReadOnlyViolation):
        assert_read_only_sql(f"SELECT 1 {word} x")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select * from pnl",
        "  \n\t SELECT as_of_ts FROM pnl ORDER BY as_of_ts",
        "SELECT as_of_ts, account_value, cumulative_return, max_drawdown, "
        "sharpe_ratio, turnover FROM pnl ORDER BY as_of_ts ASC",
        "SELECT filled_ts, commission FROM fill ORDER BY filled_ts ASC",
        "WITH recent AS (SELECT * FROM pnl) SELECT count(*) FROM recent",
        "EXPLAIN SELECT 1",
        "SHOW server_version",
        "TABLE pnl",
        "VALUES (1)",
        "SELECT count(*) FROM pnl;",
        "SELECT CASE WHEN account_value > 0 THEN 'up' ELSE 'down' END AS direction FROM pnl",
        "SELECT 'delete from pnl; drop table pnl' AS note",
        "SELECT 1 -- insert into pnl where id = 1",
        "/* update pnl set account_value = 0 */ SELECT 1",
        "SELECT as_of_ts FROM pnl WHERE as_of_ts > :ts",
        # bind parameters must not trip the keyword scan
        "SELECT as_of_ts FROM pnl WHERE as_of_ts >= :start",
    ],
)
def test_the_guard_accepts_reads(sql):
    assert assert_read_only_sql(sql) == sql
    assert is_read_only_sql(sql) is True


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO pnl (as_of_ts) VALUES (now())",
        "UPDATE pnl SET account_value = 0",
        "DELETE FROM pnl",
        "DROP TABLE pnl",
        "TRUNCATE TABLE pnl",
        "CREATE TABLE scratch (id int)",
        "ALTER TABLE pnl ADD COLUMN note text",
        "GRANT SELECT ON pnl TO public",
        "REVOKE SELECT ON pnl FROM public",
        "COPY pnl FROM '/tmp/pnl.csv'",
        "VACUUM pnl",
        "SET TRANSACTION READ WRITE",
        "RESET ALL",
        "BEGIN",
        "COMMIT",
        "ROLLBACK",
        "WITH gone AS (DELETE FROM pnl RETURNING as_of_ts) SELECT * FROM gone",
        "SELECT 1; DELETE FROM pnl",
        "SELECT * FROM pnl FOR UPDATE",
        "SELECT * FROM pnl FOR SHARE",
        "SELECT * INTO backup FROM pnl",
        "   ",
        "-- insert into pnl\n",
        "/* nothing here */",
    ],
)
def test_the_guard_refuses_writes_and_the_shapes_that_hide_them(sql):
    with pytest.raises(ReadOnlyViolation):
        assert_read_only_sql(sql)
    assert is_read_only_sql(sql) is False


def test_a_literal_that_spells_a_write_does_not_fail_the_guard():
    sql = "SELECT 'DELETE FROM fill' AS note, 'insert' AS other"
    assert assert_read_only_sql(sql) == sql


def test_a_comment_that_spells_a_write_does_not_fail_the_guard():
    sql = "SELECT 1 /* drop table pnl */ -- truncate pnl\n"
    assert assert_read_only_sql(sql) == sql


def test_a_write_after_a_semicolon_is_refused():
    with pytest.raises(ReadOnlyViolation):
        assert_read_only_sql("SELECT 1; UPDATE pnl SET account_value = 0")


def test_an_empty_statement_is_refused():
    with pytest.raises(ReadOnlyViolation):
        assert_read_only_sql("")


def test_a_non_string_statement_is_a_type_error():
    with pytest.raises(TypeError):
        assert_read_only_sql(None)
    with pytest.raises(TypeError):
        assert_read_only_sql(1)


def test_is_read_only_sql_is_false_for_a_non_statement():
    assert is_read_only_sql(None) is False


def test_is_read_only_violation_recognises_the_databases_refusal():
    assert is_read_only_violation(
        Exception("cannot execute INSERT in a read-only transaction")
    )
    assert is_read_only_violation(Exception("cannot execute DELETE in a read only transaction"))
    assert not is_read_only_violation(Exception("connection refused"))


# --------------------------------------------------------------------------
# the database itself
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_database_refuses_a_write_even_when_the_guard_is_bypassed():
    ts = _ts(10)
    async with read_only_session() as ro:
        with pytest.raises(SQLAlchemyError) as excinfo:
            await ro.raw.execute(
                text(
                    "INSERT INTO pnl (as_of_ts, account_value, cumulative_return) "
                    "VALUES (:ts, 1000, 0)"
                ),
                {"ts": ts},
            )
    message = str(excinfo.value).lower()
    assert "read-only" in message or "read only" in message
    assert await _pnl_count(ts) == 0


@pytest.mark.asyncio
async def test_the_database_refuses_a_delete_even_when_the_guard_is_bypassed():
    async with read_only_session() as ro:
        with pytest.raises(SQLAlchemyError):
            await ro.raw.execute(text("DELETE FROM pnl"))


@pytest.mark.asyncio
async def test_the_database_refuses_ddl_even_when_the_guard_is_bypassed():
    async with read_only_session() as ro:
        with pytest.raises(SQLAlchemyError):
            await ro.raw.execute(text("CREATE TABLE paper_read_only_probe (id int)"))


@pytest.mark.asyncio
async def test_a_write_through_the_facade_never_reaches_the_database():
    ts = _ts(11)
    async with read_only_session() as ro:
        with pytest.raises(ReadOnlyViolation):
            await ro.execute(
                text(
                    "INSERT INTO pnl (as_of_ts, account_value, cumulative_return) "
                    "VALUES (:ts, 1000, 0)"
                ),
                {"ts": ts},
            )
    assert await _pnl_count(ts) == 0


@pytest.mark.asyncio
async def test_the_session_still_reads_after_a_refused_statement():
    async with read_only_session() as ro:
        with pytest.raises(ReadOnlyViolation):
            await ro.execute("UPDATE pnl SET account_value = 0")
        assert await ro.scalar(text("SELECT 1")) == 1


@pytest.mark.asyncio
async def test_the_facade_refuses_an_empty_statement():
    async with read_only_session() as ro:
        with pytest.raises(ReadOnlyViolation):
            await ro.execute("   ")


# --------------------------------------------------------------------------
# reads that the dashboard needs
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_all_reads_committed_rows_oldest_first():
    for i, value in enumerate([1000.0, 1010.0, 1005.0]):
        await _insert_pnl(_ts(20 + i), account_value=value, cumulative_return=i * 0.01)

    async with read_only_session() as ro:
        rows = await ro.fetch_all(
            text(
                "SELECT as_of_ts, account_value FROM pnl "
                "WHERE as_of_ts >= :start ORDER BY as_of_ts ASC"
            ),
            {"start": _ts(20)},
        )

    assert len(rows) == 3
    assert [float(row["account_value"]) for row in rows] == [1000.0, 1010.0, 1005.0]
    assert rows[0]["as_of_ts"] is not None


@pytest.mark.asyncio
async def test_fetch_all_on_an_empty_range_returns_no_rows():
    async with read_only_session() as ro:
        rows = await ro.fetch_all(
            text("SELECT as_of_ts FROM pnl WHERE as_of_ts >= :start"),
            {"start": _ts(900)},
        )
    assert rows == []


@pytest.mark.asyncio
async def test_fetch_one_returns_a_dict_or_none():
    await _insert_pnl(_ts(30), account_value=2222.0)
    async with read_only_session() as ro:
        hit = await ro.fetch_one(
            text("SELECT account_value FROM pnl WHERE as_of_ts = :ts"), {"ts": _ts(30)}
        )
        miss = await ro.fetch_one(
            text("SELECT account_value FROM pnl WHERE as_of_ts = :ts"), {"ts": _ts(31)}
        )
    assert hit is not None and float(hit["account_value"]) == 2222.0
    assert miss is None


@pytest.mark.asyncio
async def test_scalar_reads_a_count():
    for i in range(2):
        await _insert_pnl(_ts(40 + i))
    async with read_only_session() as ro:
        value = await ro.scalar(
            text("SELECT count(*) FROM pnl WHERE as_of_ts >= :start"), {"start": _ts(40)}
        )
    assert int(value) == 2


@pytest.mark.asyncio
async def test_a_plain_string_statement_works_like_a_text_clause():
    await _insert_pnl(_ts(50), account_value=1234.5)
    async with read_only_session() as ro:
        rows = await ro.fetch_all(
            "SELECT account_value FROM pnl WHERE as_of_ts = :ts", {"ts": _ts(50)}
        )
    assert len(rows) == 1 and float(rows[0]["account_value"]) == 1234.5


# --------------------------------------------------------------------------
# leaving the read-only path
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_writes_still_work_after_the_read_only_session_closes():
    ts = _ts(60)
    async with read_only_session() as ro:
        assert await ro.scalar(text("SELECT 1")) == 1

    await _insert_pnl(ts, account_value=1500.0)
    assert await _pnl_count(ts) == 1


@pytest.mark.asyncio
async def test_two_read_only_sessions_can_run_side_by_side():
    await _insert_pnl(_ts(70), account_value=10.0)
    async with read_only_session() as first:
        async with read_only_session() as second:
            assert int(await first.scalar(text("SELECT count(*) FROM pnl"))) >= 1
            assert int(await second.scalar(text("SELECT count(*) FROM pnl"))) >= 1
