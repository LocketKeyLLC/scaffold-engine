"""§17.484 — persistent per-role model override module tests.

Mock-DB level: set/clear mutate the live settings singleton AND issue the
persist/delete; load replays rows onto settings. The in-process validation is
covered by config.set_runtime_model tests (test_model_valves.py)."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import config
from app.modules import model_overrides as mo


def _mock_db(rows=None):
    res = MagicMock()
    res.mappings.return_value.all.return_value = rows or []
    db = AsyncMock()
    db.execute = AsyncMock(return_value=res)
    db.commit = AsyncMock()
    return db


@pytest.mark.smoke
async def test_set_override_persists_and_mutates():
    db = _mock_db()
    original = config.settings.model_general
    try:
        await mo.set_override("model_general", "persist:7b", db)
        assert config.settings.model_general == "persist:7b"   # in-process applied
        assert db.execute.await_count == 1                     # row upserted
        assert db.commit.await_count == 1
    finally:
        config.settings.model_general = original


@pytest.mark.smoke
async def test_set_override_rejects_locked_role_no_write():
    db = _mock_db()
    original = config.settings.model_reranker
    with pytest.raises(ValueError):
        await mo.set_override("model_reranker", "x:1b", db)
    assert config.settings.model_reranker == original
    assert db.execute.await_count == 0                         # never wrote a row


@pytest.mark.smoke
async def test_clear_override_reverts_and_deletes():
    db = _mock_db()
    env_def = config.env_default_model("model_coder")
    config.settings.model_coder = "temp:1b"                    # pretend overridden
    try:
        await mo.clear_override("model_coder", db)
        assert config.settings.model_coder == env_def
        assert db.execute.await_count == 1                     # DELETE issued
        assert db.commit.await_count == 1
    finally:
        config.settings.model_coder = env_def


@pytest.mark.smoke
async def test_list_overrides():
    db = _mock_db(rows=[{"role": "model_general", "model": "x:7b"},
                        {"role": "model_coder", "model": "y:3b"}])
    assert await mo.list_overrides(db) == {"model_general": "x:7b", "model_coder": "y:3b"}


@pytest.mark.smoke
async def test_load_overrides_applies_and_skips_invalid():
    db = _mock_db(rows=[{"role": "model_general", "model": "loaded:7b"},
                        {"role": "model_bogus", "model": "z:1b"}])  # bogus skipped, not crashing
    original = config.settings.model_general
    try:
        n = await mo.load_overrides_into_settings(db)
        assert n == 1
        assert config.settings.model_general == "loaded:7b"
    finally:
        config.settings.model_general = original
