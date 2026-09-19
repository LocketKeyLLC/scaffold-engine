"""§17.1132 (ledger L-5) — a swallowed DB error must not doom the request session.

The shape: ``try: await db.execute(...)  except Exception: log`` and then more
statements on the same session. After the first failure Postgres has aborted
the transaction; the next statement raises ``PendingRollbackError`` ("Can't
reconnect until invalid transaction is rolled back") and the operator's request
500s — one live occurrence (2026-09-13, submit → _maybe_finalize_session, the
compile step failed with an InterfaceError and finalization continued).

Rule, enforced statically here: optional statements run inside
``async with savepoint(db):`` (``app.utils.savepoint.savepoint`` → ``begin_nested``, a SAVEPOINT — a failure discards only that
work and the session stays usable); a swallowed failed ``commit`` must
``await db.rollback()`` in its handler. The DB-level test proves the pattern
against a real Postgres.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DB_CALLS = ("execute", "commit", "flush", "scalar", "scalars", "get", "run_sync")
SESSION_FACTORIES = ("async_session", "get_session")


def _session_names(fn):
    names = {a.arg for a in fn.args.args + fn.args.kwonlyargs + fn.args.posonlyargs} & {"db", "session", "conn"}
    for n in ast.walk(fn):
        if isinstance(n, ast.AsyncWith):
            for item in n.items:
                ctx = item.context_expr
                if (isinstance(item.optional_vars, ast.Name) and isinstance(ctx, ast.Call)
                        and getattr(ctx.func, "id", "") in SESSION_FACTORIES):
                    names.add(item.optional_vars.id)
    return names


def _db_call_on(node, names):
    for n in ast.walk(node):
        if isinstance(n, ast.Await) and isinstance(n.value, ast.Call):
            f = n.value.func
            if (isinstance(f, ast.Attribute) and f.attr in DB_CALLS
                    and isinstance(f.value, ast.Name) and f.value.id in names):
                return f.value.id
    return None


def _has(node, attr):
    return any(isinstance(n, ast.Attribute) and n.attr == attr for n in ast.walk(node))


def _calls_savepoint(node):
    return any(isinstance(n, ast.Call) and getattr(n.func, "id", "") == "savepoint" for n in ast.walk(node))


def _reraises(handler):
    return any(isinstance(n, (ast.Raise, ast.Return)) for n in ast.walk(handler))


def find_swallow_then_reuse(root: Path = ROOT):
    hits = []
    for f in sorted((root / "app").rglob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            names = _session_names(fn)
            if not names:
                continue
            for parent in ast.walk(fn):
                for field in ("body", "orelse", "finalbody"):
                    seq = getattr(parent, field, None)
                    if not isinstance(seq, list):
                        continue
                    for i, stmt in enumerate(seq):
                        if not isinstance(stmt, ast.Try):
                            continue
                        body = ast.Module(body=stmt.body, type_ignores=[])
                        name = _db_call_on(body, names)
                        if not name or _has(body, "begin_nested") or _calls_savepoint(body):
                            continue
                        swallowing = [h for h in stmt.handlers if not _has(h, "rollback") and not _reraises(h)]
                        if not swallowing:
                            continue
                        # reuse means the SAME session name later in this statement list — a
                        # block that opened its own short-lived session is not the shape
                        if not any(_db_call_on(s, {name}) for s in seq[i + 1:]):
                            continue
                        hits.append(f"{f.relative_to(root)}:{stmt.lineno} {fn.name}")
    return hits


def test_no_swallowed_db_error_is_followed_by_session_reuse():
    hits = find_swallow_then_reuse()
    assert not hits, (
        "a swallowed DB error followed by more statements on the same session — "
        "wrap the optional statements in `async with savepoint(db):` (app.utils.savepoint) (or "
        "`await db.rollback()` after a swallowed commit):\n" + "\n".join(hits)
    )


def test_the_scanner_recognises_the_shape(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "m.py").write_text(
        "async def f(db):\n"
        "    try:\n        await db.execute('x')\n    except Exception:\n        pass\n"
        "    await db.execute('y')\n"
        "async def g(db):\n"
        "    try:\n        async with savepoint(db):\n            await db.execute('x')\n"
        "    except Exception:\n        pass\n    await db.execute('y')\n"
        "async def h(db):\n"
        "    try:\n        await db.commit()\n    except Exception:\n        await db.rollback()\n"
        "    await db.execute('y')\n"
        "async def k():\n"
        "    async with async_session() as db:\n"
        "        try:\n            await db.execute('x')\n        except Exception:\n            pass\n"
        "        await db.execute('y')\n"
        "async def own(db):\n"
        "    try:\n        async with async_session() as s2:\n            await s2.execute('x')\n"
        "    except Exception:\n        pass\n    await db.execute('y')\n"
    )
    hits = find_swallow_then_reuse(tmp_path)
    assert [h.split(" ")[1] for h in hits] == ["f", "k"], hits  # g: savepoint · h: rollback · own: its own session


@pytest.mark.asyncio
async def test_savepoint_keeps_the_session_usable_after_a_failed_statement():
    """Against the real (test) Postgres: the L-5 shape reproduces without a
    savepoint and is gone with one."""
    import os
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError, PendingRollbackError
    if "_test" not in (os.environ.get("DATABASE_URL") or ""):
        pytest.skip("needs the test database (container lane)")
    from app.database import async_session

    async with async_session() as db:
        await db.execute(text("SELECT 1"))
        try:
            await db.execute(text("SELECT 1/0"))
        except DBAPIError:
            pass
        # the session is unusable: asyncpg reports Postgres's "current transaction is
        # aborted" on the first reuse, SQLAlchemy's PendingRollbackError after that
        with pytest.raises((DBAPIError, PendingRollbackError)) as ei:
            await db.execute(text("SELECT 1"))
        assert "aborted" in str(ei.value) or "rolled back" in str(ei.value)
        await db.rollback()

    async with async_session() as db:
        await db.execute(text("SELECT 1"))
        try:
            async with db.begin_nested():
                await db.execute(text("SELECT 1/0"))
        except DBAPIError:
            pass
        assert (await db.execute(text("SELECT 41 + 1"))).scalar() == 42
        await db.rollback()


def test_savepoint_helper_degrades_to_a_noop_on_a_mock_session():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    from app.utils.savepoint import savepoint

    async def run(db):
        async with savepoint(db):
            return "ran"
    assert asyncio.run(run(AsyncMock())) == "ran"      # begin_nested() is a coroutine child → no-op
    assert asyncio.run(run(MagicMock(spec=[]))) == "ran"  # no begin_nested at all → no-op
