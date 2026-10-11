"""§17.1467 — `ORDER BY created_at` on assist_sessions, a table with no such column.

Live (2026-10-11): §17.1466's address watch picked the job's session with `… ORDER BY created_at DESC` and failed
on the first real move (`column "created_at" does not exist … Perhaps you meant "assist_sessions.updated_at"`). The
same query had sat in machine_truth's reinstall fact writer since §17.1315, swallowed by its fail-soft `except`.
"""
import pathlib
import re

COLS = set(re.findall(r"^\s+(\w+)\s", re.search(r"CREATE TABLE[^(]*\bassist_sessions\s*\(([\s\S]*?)\n\);",
                                                 pathlib.Path("db/init.sql").read_text()).group(1), re.M))


def test_the_baseline_has_no_created_at():
    assert "updated_at" in COLS and "started_at" in COLS and "created_at" not in COLS


def test_no_query_orders_assist_sessions_by_a_column_it_lacks():
    bad = []
    for p in pathlib.Path("app").rglob("*.py"):
        for m in re.finditer(r"FROM\s+assist_sessions\b[^\"')]*?ORDER BY\s+(\w+)", p.read_text()):
            if m.group(1) not in COLS:
                bad.append(f"{p}: ORDER BY {m.group(1)}")
    assert not bad, bad
