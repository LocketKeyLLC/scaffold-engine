"""
conftest.py — Shared fixtures for Scaffold Engine test suite.

NOTE (#9.8/#9.9): These top-level imports are load-bearing. Removing them
causes every `from app.X import ...` in a test module to fail with
"'app' is not a package" because pytest's path finder needs `app` as a
resolved package before test modules reference submodules like
`app.utils.embedding`. Attempted removal on 2026-04-21 caused 16 collection
errors — see drift-findings.md. If you really need to drop these, first
convert the test suite to use `tests/` as a proper package (add
`tests/__init__.py` and configure rootdir). Until then, keep them.
"""
import app  # noqa: F401  — load-bearing; see note above
import app.model_router  # noqa: F401  — load-bearing; see note above
import pytest
from unittest.mock import AsyncMock, MagicMock


def make_mock_db(rows: list[dict] | None = None, *, scalar=None, rowcount=None):
    """
    Build a mock AsyncSession whose .execute() returns a result object
    compatible with the common SQLAlchemy access patterns (#9.10):

      result.mappings().all()  -> rows (list of dicts)
      result.fetchall()        -> rows
      result.scalar()          -> `scalar` (or rows[0] if single-col row)
      result.scalar_one()      -> same as scalar()
      result.scalar_one_or_none() -> same as scalar()
      result.first()           -> rows[0] (or None if empty)
      result.rowcount          -> `rowcount` (default: len(rows))

    Args:
        rows: List of dicts representing the result set.
        scalar: Explicit scalar return value (overrides row-based inference).
        rowcount: Explicit rowcount override.
    """
    rows = rows or []

    mappings_obj = MagicMock()
    mappings_obj.all.return_value = rows

    result_obj = MagicMock()
    result_obj.mappings.return_value = mappings_obj
    result_obj.fetchall.return_value = rows
    result_obj.first.return_value = rows[0] if rows else None
    # scalar() / scalar_one() / scalar_one_or_none() all return the same mock value
    inferred_scalar = scalar
    if inferred_scalar is None and rows:
        first = rows[0]
        # For single-column rows represented as {"col": value} or (value,) tuples
        if isinstance(first, dict) and len(first) == 1:
            inferred_scalar = next(iter(first.values()))
        else:
            inferred_scalar = first
    result_obj.scalar.return_value = inferred_scalar
    result_obj.scalar_one.return_value = inferred_scalar
    result_obj.scalar_one_or_none.return_value = inferred_scalar
    result_obj.rowcount = rowcount if rowcount is not None else len(rows)

    db = AsyncMock()
    db.execute.return_value = result_obj
    return db
