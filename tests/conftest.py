"""
conftest.py — Shared fixtures for Scaffold Engine test suite.
"""
import app  # noqa: F401  — pre-load real package before stub-based tests
import app.model_router  # noqa: F401
import pytest
from unittest.mock import AsyncMock, MagicMock


def make_mock_db(rows: list[dict]):
    """
    Build a mock AsyncSession whose .execute() returns rows
    compatible with SQLAlchemy's .mappings().all() pattern.
    """
    mappings_obj = MagicMock()
    mappings_obj.all.return_value = rows
    result_obj = MagicMock()
    result_obj.mappings.return_value = mappings_obj
    db = AsyncMock()
    db.execute.return_value = result_obj
    return db
