"""§17.1819 — in the engine-test sandbox every test starts with the job's own tables empty.

Live (follow-up 5b4880c0 T1F1, the P&L calculator): one scratch database per run, shared by all its tests, and the
job's audit tables append-only (UPDATE/DELETE refused by trigger) -- "no fills" saw the previous test's AAPL position
and realized P&L summed across tests (20, 50, 70, 90). Seven correct tests failed together.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "docker" / "enginetest" / "server.py"


def _server():
    if not SERVER.exists():
        pytest.skip("docker/ is not in this image")
    spec = importlib.util.spec_from_file_location("enginetest_server_1819", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Conn:
    def __init__(self, names):
        self.names = names

    async def fetch(self, q):
        assert "pg_tables" in q
        return [{"tablename": n} for n in self.names]


@pytest.mark.asyncio
async def test_the_jobs_tables_are_those_its_revisions_added():
    srv = _server()
    got = await srv.job_tables(_Conn(["jobs", "dag_nodes", "proposal", "fill", "alembic_version"]),
                               _Conn(["jobs", "dag_nodes", "alembic_version"]))
    assert got == ["fill", "proposal"]                                  # never the engine's own (seeded) tables


@pytest.mark.integration
async def test_a_test_does_not_see_the_rows_the_test_before_it_wrote(tmp_path):
    """Real pytest, real Postgres, an append-only table as the lab's migration makes it."""
    import asyncpg

    from app.config import settings
    srv = _server()
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    table = f"iso_probe_{uuid.uuid4().hex[:8]}"
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(f'CREATE TABLE "{table}" (id serial PRIMARY KEY, v int NOT NULL)')
        await conn.execute("CREATE OR REPLACE FUNCTION iso_forbid() RETURNS trigger LANGUAGE plpgsql AS "
                           "$$ BEGIN RAISE EXCEPTION 'append-only'; END $$")
        await conn.execute(f'CREATE TRIGGER no_delete BEFORE UPDATE OR DELETE ON "{table}" '
                           "FOR EACH ROW EXECUTE FUNCTION iso_forbid()")
        (tmp_path / "_enginetest_isolation.py").write_text(srv.ISOLATION_PLUGIN)
        (tmp_path / "test_two.py").write_text(
            "import asyncio, os, asyncpg\n"
            "async def _q(sql):\n"
            "    c = await asyncpg.connect(os.environ['ENGINETEST_DB_DSN'])\n"
            "    try:\n        return await c.fetchval(sql)\n    finally:\n        await c.close()\n"
            f"def test_a_writes():\n    asyncio.run(_q('INSERT INTO \"{table}\" (v) VALUES (1) RETURNING id'))\n"
            f"def test_b_starts_empty():\n    assert asyncio.run(_q('SELECT count(*) FROM \"{table}\"')) == 0\n"
            f"def test_c_ids_restart():\n"
            f"    assert asyncio.run(_q('INSERT INTO \"{table}\" (v) VALUES (2) RETURNING id')) == 1\n")
        env = {**os.environ, "ENGINETEST_JOB_TABLES": table, "ENGINETEST_DB_DSN": dsn,
               "PYTEST_PLUGINS": "_enginetest_isolation", "PYTHONPATH": str(tmp_path)}
        run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", "/dev/null",
                              "--rootdir", str(tmp_path), str(tmp_path / "test_two.py")],
                             cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
        assert run.returncode == 0, run.stdout + run.stderr
        assert "3 passed" in run.stdout
        env.pop("PYTEST_PLUGINS")                                       # without the plugin: the leak, as live
        leak = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", "/dev/null",
                               "--rootdir", str(tmp_path), str(tmp_path / "test_two.py")],
                              cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
        assert leak.returncode != 0 and "test_b_starts_empty" in leak.stdout
    finally:
        await conn.execute(f'DROP TABLE IF EXISTS "{table}"')
        await conn.execute("DROP FUNCTION IF EXISTS iso_forbid()")
        await conn.close()


@pytest.mark.asyncio
async def test_a_database_run_loads_the_plugin_with_the_jobs_tables():
    from unittest.mock import AsyncMock, patch
    srv = _server()
    seen: dict = {}

    class _Proc:
        returncode = 0
        pid = 1

        async def communicate(self):
            return b"1 passed", b""

        async def wait(self):
            return 0

    async def _exec(*argv, cwd=None, env=None, **kw):
        if argv[:2] != ("python", "-c"):                               # the test command, not the alembic upgrade
            seen["env"], seen["plugin"] = dict(env), (Path(cwd) / "_enginetest_isolation.py").read_text()
        return _Proc()
    srv.DB_DSN = "postgresql://sandbox:sandbox@db:5432"
    srv._base.update(ok=True)
    with patch.object(srv, "_ensure_base", new=AsyncMock()), \
         patch.object(srv, "_fresh_db", new=AsyncMock(return_value="t_abc")), \
         patch.object(srv, "_drop_db", new=AsyncMock()), \
         patch.object(srv, "_job_tables_of", new=AsyncMock(return_value=["proposal", "fill"])), \
         patch.object(srv.asyncio, "create_subprocess_exec", new=_exec):
        out = await srv.run(srv.RunRequest(files={"tests/test_x.py": "def test_x(): pass"},
                                           command=["python", "-m", "pytest", "tests/test_x.py"], database=True))
    assert out.ok and out.database == "ok"
    assert seen["env"]["PYTEST_PLUGINS"] == "_enginetest_isolation"
    assert seen["env"]["ENGINETEST_JOB_TABLES"] == "proposal,fill"
    assert seen["env"]["ENGINETEST_DB_DSN"] == "postgresql://sandbox:sandbox@db:5432/t_abc"
    assert seen["plugin"] == srv.ISOLATION_PLUGIN
