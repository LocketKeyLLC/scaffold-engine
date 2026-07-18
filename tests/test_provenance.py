"""Tests for app.modules.provenance — confidence_for, build_provenance,
write_provenance, get_provenance_batch.

Pure-function tests + AsyncMock-backed round-trip for the DB writers.
A real Postgres round-trip lands in §17.106 when the first producer ships.
"""
from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.modules.provenance import (
    CONFIDENCE_BY_SOURCE,
    DEFAULT_CONFIDENCE,
    build_provenance,
    confidence_for,
    get_provenance_batch,
    write_provenance,
)


# ---------------------------------------------------------------------------
# confidence_for
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestConfidenceFor:
    @pytest.mark.parametrize("source_type,expected", list(CONFIDENCE_BY_SOURCE.items()))
    def test_known_source_types(self, source_type, expected):
        assert confidence_for(source_type) == expected

    def test_unknown_source_type_falls_back(self, caplog):
        with caplog.at_level("WARNING", logger="scaffold.provenance"):
            score = confidence_for("not_a_real_type")
        assert score == DEFAULT_CONFIDENCE
        assert any(
            "confidence_unknown_source_type" in r.getMessage()
            for r in caplog.records
        )

    def test_override_wins(self):
        assert confidence_for("test_code", override=0.3) == 0.3

    def test_override_zero_wins(self):
        # 0.0 is a valid override (not falsy-skipped).
        assert confidence_for("test_code", override=0.0) == 0.0


# ---------------------------------------------------------------------------
# build_provenance
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestBuildProvenance:
    def test_defaults(self):
        before = int(time.time())
        p = build_provenance()
        after = int(time.time())
        assert p["source_ref"] == ""
        assert before <= p["fetched_at"] <= after
        assert p["quality_signal"] == {}

    def test_explicit_values(self):
        p = build_provenance(
            source_ref="abc123",
            fetched_at=1700000000,
            quality_signal={"votes": 42, "is_accepted": True},
        )
        assert p == {
            "source_ref": "abc123",
            "fetched_at": 1700000000,
            "quality_signal": {"votes": 42, "is_accepted": True},
        }


# ---------------------------------------------------------------------------
# write_provenance + get_provenance_batch — round-trip via AsyncMock session
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_write_provenance_executes_upsert():
    session = AsyncMock()
    prov = {
        "source_ref": "release/v2.32.0",
        "fetched_at": 1700000000,
        "quality_signal": {"merged": True},
    }
    await write_provenance(session, "scaffold-test-abc12345", prov)

    session.execute.assert_called_once()
    args, _ = session.execute.call_args
    sql_obj, params = args[0], args[1]
    assert "INSERT INTO rag_entry_provenance" in str(sql_obj)
    assert "ON CONFLICT (entry_id) DO UPDATE" in str(sql_obj)
    assert params["eid"] == "scaffold-test-abc12345"
    assert params["ref"] == "release/v2.32.0"
    assert params["fa"] == 1700000000
    assert json.loads(params["qs"]) == {"merged": True}


@pytest.mark.asyncio
async def test_write_provenance_batch_single_multirow_insert():
    """§17.616 (audit #33) — one multi-row INSERT for N rows, deduped by
    entry_id (last wins) so a repeated id can't double-affect ON CONFLICT."""
    from app.modules.provenance import write_provenance_batch
    session = AsyncMock()
    rows = [
        ("e1", {"source_ref": "a"}, "h1"),
        ("e2", {"source_ref": "b"}, None),
        ("e1", {"source_ref": "a2"}, "h1b"),  # duplicate id → last wins
    ]
    await write_provenance_batch(session, rows, session_id=None)

    session.execute.assert_called_once()
    args, _ = session.execute.call_args
    sql_obj, params = str(args[0]), args[1]
    assert "INSERT INTO rag_entry_provenance" in sql_obj
    assert "ON CONFLICT (entry_id) DO UPDATE" in sql_obj
    # Two distinct entry_ids → two VALUES tuples (e1 deduped to the last).
    assert params["eid0"] == "e1" and params["ref0"] == "a2"
    assert params["eid1"] == "e2"
    assert "eid2" not in params  # the duplicate collapsed


@pytest.mark.asyncio
async def test_write_provenance_batch_empty_short_circuits():
    from app.modules.provenance import write_provenance_batch
    session = AsyncMock()
    await write_provenance_batch(session, [])
    session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_get_provenance_batch_empty_short_circuits():
    session = AsyncMock()
    out = await get_provenance_batch(session, [])
    assert out == {}
    session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_get_provenance_batch_returns_map():
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.mappings.return_value = [
        {
            "entry_id": "scaffold-foo-12345678",
            "source_ref": "abc123",
            "fetched_at": 1700000000,
            "quality_signal": {"votes": 5},  # JSONB → dict path
        },
        {
            "entry_id": "scaffold-bar-87654321",
            "source_ref": "v1.0.0",
            "fetched_at": 1700100000,
            "quality_signal": '{"points": 12}',  # raw-str fallback path
        },
    ]
    session.execute = AsyncMock(return_value=mock_result)

    out = await get_provenance_batch(
        session, ["scaffold-foo-12345678", "scaffold-bar-87654321"]
    )
    assert out["scaffold-foo-12345678"] == {
        "source_ref": "abc123",
        "fetched_at": 1700000000,
        "quality_signal": {"votes": 5},
    }
    assert out["scaffold-bar-87654321"] == {
        "source_ref": "v1.0.0",
        "fetched_at": 1700100000,
        "quality_signal": {"points": 12},
    }
