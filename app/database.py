"""Async database connection and session management."""
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

engine = create_async_engine(
    settings.database_url,
    echo=(settings.log_level.lower() == "debug"),
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
    # §17.179 — cap the asyncpg connect handshake at 5 s. Default is
    # 60 s, which under an unreachable Postgres host (e.g. cloud-CI
    # smoke where `scaffold-postgres` is NXDOMAIN) makes every
    # async_session() open block for a full minute before raising.
    # Healthy connects complete in milliseconds; 5 s is ample. This
    # is the connect timeout only — per-query budgets stay unbounded.
    connect_args={"timeout": 5},
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields an async database session.

    Callers commit explicitly. Teardown only rolls back on unhandled exception.
    """
    async with async_session() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
