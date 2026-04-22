"""Shared fixtures and helpers for test_execution_agent_*.py files (#9.6).

All module-level imports and helper functions from the original
test_execution_agent.py live here, so split files can `from ... import *`.

Leading underscore in the filename -> pytest skips collection.
"""

__all__ = [
    "_collect_sse",
    "_make_sse_db",
    "_make_sse_db_guard_fails",
    # Also re-export anything else tests reach for from module top:
    "pytest", "asyncio", "httpx", "json", "MagicMock", "AsyncMock", "patch",
    "make_mock_db",
]

"""
test_execution_agent.py — Unit tests for execution_agent module.

Phase 1: _compile_output() 3-strategy priority chain + partial compile.
Run:  docker exec scaffold-orchestrator pytest tests/test_execution_agent.py -m smoke --timeout=30 -v
"""


import httpx


import pytest


import asyncio


from tests.conftest import make_mock_db


import json


from unittest.mock import patch, AsyncMock, MagicMock


async def _collect_sse(coro):
    """Drain an async generator and return list of (event, data) tuples.

    Must be awaited by the caller; tests are async def so this works
    natively under asyncio_mode=auto (#9.11).
    """
    events = []
    async for chunk in coro:
        for block in chunk.strip().split("\n\n"):
            lines = block.strip().split("\n")
            event = None
            data = None
            for line in lines:
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: "):
                    data = json.loads(line[6:])
            events.append((event, data))
    return events


def _make_sse_db(dag_node_count=2):
    """Mock db + async_session for execute_all_nodes."""
    scalar_result = MagicMock()
    scalar_result.scalar.return_value = dag_node_count
    guard_result = MagicMock()
    guard_result.rowcount = 1
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[guard_result] + [scalar_result] * 20)
    db.commit = AsyncMock()
    mock_session_ctx = AsyncMock()
    mock_session_ctx.__aenter__ = AsyncMock(return_value=db)
    mock_session_ctx.__aexit__ = AsyncMock(return_value=False)
    mock_async_session = MagicMock(return_value=mock_session_ctx)
    return db, mock_async_session


def _make_sse_db_guard_fails():
    """Mock where guard fails (job not found)."""
    guard_result = MagicMock()
    guard_result.rowcount = 0
    db = AsyncMock()
    db.execute = AsyncMock(return_value=guard_result)
    db.commit = AsyncMock()
    mock_session_ctx = AsyncMock()
    mock_session_ctx.__aenter__ = AsyncMock(return_value=db)
    mock_session_ctx.__aexit__ = AsyncMock(return_value=False)
    mock_async_session = MagicMock(return_value=mock_session_ctx)
    return db, mock_async_session

    return db

