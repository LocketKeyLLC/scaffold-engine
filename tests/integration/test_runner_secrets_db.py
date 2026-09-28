"""§17.1193 — the engine's own secret store, against real Postgres.

§17.1191 refused to hold a password and made the operator write it into a file
on the target by hand. Every tool this engine is measured against — Ansible
Vault, GitHub Actions secrets, Jenkins credentials, Vault — asks once, keeps
the value encrypted and injects it at run time. The property that mattered
(the value never enters the command, the approval, the log line or the process
table) comes from delivering it out of band, not from refusing to hold it.

These run against the throwaway test database because encryption at rest and a
CHECK constraint on the name are exactly the parts a mock cannot tell you
about.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.database import async_session
from app.modules import runner_secrets as rs

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.timeout(120)]


@pytest.fixture
async def clean():
    async with async_session() as db:
        await db.execute(text("DELETE FROM runner_secrets WHERE name LIKE 'ITEST_%'"))
        await db.commit()
    yield
    async with async_session() as db:
        await db.execute(text("DELETE FROM runner_secrets WHERE name LIKE 'ITEST_%'"))
        await db.commit()


async def test_a_value_round_trips_and_is_not_stored_in_the_clear(clean):
    async with async_session() as db:
        await rs.set_secret(db, "ITEST_DB_PASSWORD", "hunter2-correct-horse", hint="the panel's admin password")
        row = (await db.execute(text(
            "SELECT value_enc, hint FROM runner_secrets WHERE name = 'ITEST_DB_PASSWORD'"))).mappings().first()
        assert row is not None
        assert "hunter2" not in row["value_enc"], "the value is readable straight out of the table"
        assert row["hint"] == "the panel's admin password"
        assert await rs.values_for(db, ["ITEST_DB_PASSWORD"]) == {"ITEST_DB_PASSWORD": "hunter2-correct-horse"}


async def test_listing_never_returns_a_value(clean):
    async with async_session() as db:
        await rs.set_secret(db, "ITEST_TOKEN", "tok-abc-123")
        listed = [s for s in await rs.list_secrets(db) if s["name"].startswith("ITEST_")]
        assert [s["name"] for s in listed] == ["ITEST_TOKEN"]
        blob = repr(listed)
        assert "tok-abc-123" not in blob, blob
        assert "value" not in listed[0] and "value_enc" not in listed[0]


async def test_storing_the_same_name_twice_replaces_the_value(clean):
    async with async_session() as db:
        await rs.set_secret(db, "ITEST_TOKEN", "first")
        await rs.set_secret(db, "ITEST_TOKEN", "second")
        assert await rs.values_for(db, ["ITEST_TOKEN"]) == {"ITEST_TOKEN": "second"}
        n = (await db.execute(text("SELECT count(*) FROM runner_secrets WHERE name='ITEST_TOKEN'"))).scalar()
        assert n == 1


async def test_only_the_names_asked_for_come_back(clean):
    async with async_session() as db:
        await rs.set_secret(db, "ITEST_A", "a-value")
        await rs.set_secret(db, "ITEST_B", "b-value")
        assert await rs.values_for(db, ["ITEST_A"]) == {"ITEST_A": "a-value"}
        assert await rs.values_for(db, []) == {}
        assert await rs.values_for(db, ["ITEST_NOPE"]) == {}


async def test_a_bad_name_is_refused_by_the_column_not_only_the_helper(clean):
    async with async_session() as db:
        for bad in ("lower_case", "1LEADING", "has space", "has-dash", ""):
            with pytest.raises(ValueError):
                await rs.set_secret(db, bad, "x")
        with pytest.raises(ValueError):
            await rs.set_secret(db, "ITEST_OK", "   ")
        # and the database refuses it too, so a future caller cannot slip past
        with pytest.raises(Exception):
            await db.execute(text("INSERT INTO runner_secrets (name, value_enc) VALUES ('lower', 'x')"))
        await db.rollback()


async def test_forgetting_a_value_removes_it(clean):
    async with async_session() as db:
        await rs.set_secret(db, "ITEST_GONE", "x")
        assert await rs.delete_secret(db, "ITEST_GONE") is True
        assert await rs.values_for(db, ["ITEST_GONE"]) == {}
        assert await rs.delete_secret(db, "ITEST_GONE") is False


async def test_use_is_stamped_so_a_stale_value_is_visible(clean):
    async with async_session() as db:
        await rs.set_secret(db, "ITEST_USED", "x")
        before = (await db.execute(text(
            "SELECT last_used_at FROM runner_secrets WHERE name='ITEST_USED'"))).scalar()
        assert before is None
        await rs.values_for(db, ["ITEST_USED"])
        after = (await db.execute(text(
            "SELECT last_used_at FROM runner_secrets WHERE name='ITEST_USED'"))).scalar()
        assert after is not None
