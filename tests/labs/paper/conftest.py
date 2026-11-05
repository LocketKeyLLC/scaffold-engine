"""The paper lab's tests, delivered by `scripts/deliver_job.py` (§17.1932).

Each test starts with the lab's own tables empty, as the engine's sandbox ran them (§17.1819): the tests insert the
rows they need and count what the code wrote. The tables are the job's (its Alembic revision's), never the engine's."""
from __future__ import annotations

import asyncio

import pytest

TABLES = ['ingested_item', 'signal', 'proposal', 'risk_check', 'approval', 'broker_order_id', 'fill', 'pnl']


async def _empty() -> None:
    import asyncpg

    from app.config import settings
    conn = await asyncpg.connect(settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1))
    try:
        await conn.execute("TRUNCATE " + ", ".join(f'"{t}"' for t in TABLES) + " RESTART IDENTITY CASCADE")
    finally:
        await conn.close()


@pytest.fixture(autouse=True)
def _lab_tables_empty():
    if TABLES:
        asyncio.run(_empty())
    yield
