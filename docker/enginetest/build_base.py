"""§17.1783 — build the engine-test sandbox's template database: the engine's schema exactly as the engine builds
its own (db/init.sql, then the SQL migration runner, then `alembic upgrade head`), from the code baked into this
image. Each test run copies it (CREATE DATABASE … TEMPLATE) and applies the job's own revision on top.

Run once per sandbox process, with DATABASE_URL pointing at the template database (created here if missing).
Exit 0 on success; anything else is a reason the sandbox runs without a database.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from urllib.parse import urlparse


async def main() -> int:
    url = os.environ["DATABASE_URL"]                       # postgresql+asyncpg://user:pw@host:port/engine_base
    u = urlparse(url.replace("+asyncpg", ""))
    name = u.path.lstrip("/")
    import asyncpg
    admin = await asyncpg.connect(user=u.username, password=u.password, host=u.hostname, port=u.port or 5432,
                                  database="postgres")
    try:
        exists = await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name)
        if exists:
            await admin.execute(f'ALTER DATABASE "{name}" IS_TEMPLATE false')
            await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    conn = await asyncpg.connect(user=u.username, password=u.password, host=u.hostname, port=u.port or 5432,
                                 database=name)
    try:
        await conn.execute(Path("/code/db/init.sql").read_text(encoding="utf-8"))
    finally:
        await conn.close()
    from app.alembic_runner import run_alembic_upgrade
    from app.migrations import run_migrations
    sql = await run_migrations()
    if sql.get("status") == "error":
        print("sql migrations failed:", sql, file=sys.stderr)
        return 2
    alembic = await run_alembic_upgrade()
    if alembic.get("status") != "ok":
        print("alembic failed:", alembic, file=sys.stderr)
        return 3
    admin = await asyncpg.connect(user=u.username, password=u.password, host=u.hostname, port=u.port or 5432,
                                  database="postgres")
    try:
        await admin.execute(f'ALTER DATABASE "{name}" IS_TEMPLATE true')
    finally:
        await admin.close()
    print("base ready:", alembic.get("after"))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
