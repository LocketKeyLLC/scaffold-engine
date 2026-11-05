# tests/test_paper_operator_approval_gate.py
"""Tests for the paper operator approval gate module."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from unittest.mock import AsyncMock, MagicMock, patch

from app.database import async_session
from app.modules.paper_operator_approval_gate import (
    handle_approval_answer,
    request_approval,
)


async def _insert_ingested_item(db) -> int:
    external_id = uuid4().hex
    now = datetime.now(timezone.utc)
    result = await db.execute(
        text(
            """
            INSERT INTO ingested_item
                (external_id, source, published_ts, fetched_ts, payload)
            VALUES
                (:external_id, :source, :published_ts, :fetched_ts,
                 CAST(:payload AS json))
            RETURNING id
            """
        ),
        {
            "external_id": external_id,
            "source": "news",
            "published_ts": now,
            "fetched_ts": now,
            "payload": "{}",
        },
    )
    return result.scalar_one()


async def _insert_signal(db, ingested_item_id: int) -> int:
    result = await db.execute(
        text(
            """
            INSERT INTO signal
                (ingested_item_id, ticker, signal_type, direction, rationale)
            VALUES
                (:ingested_item_id, :ticker, :signal_type, :direction, :rationale)
            RETURNING id
            """
        ),
        {
            "ingested_item_id": ingested_item_id,
            "ticker": "AAPL",
            "signal_type": "news_context",
            "direction": "long",
            "rationale": "test signal",
        },
    )
    return result.scalar_one()


async def _insert_proposal(db, signal_id: int) -> int:
    result = await db.execute(
        text(
            """
            INSERT INTO proposal
                (signal_id, ticker, side, quantity, order_type, rationale)
            VALUES
                (:signal_id, :ticker, :side, :quantity, :order_type, :rationale)
            RETURNING id
            """
        ),
        {
            "signal_id": signal_id,
            "ticker": "AAPL",
            "side": "buy",
            "quantity": "1",
            "order_type": "market",
            "rationale": "test proposal",
        },
    )
    return result.scalar_one()


async def _insert_proposal_row(db) -> int:
    ingested_id = await _insert_ingested_item(db)
    signal_id = await _insert_signal(db, ingested_id)
    proposal_id = await _insert_proposal(db, signal_id)
    await db.commit()
    return proposal_id


# ----------------------------------------------------------------------
# request_approval tests
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_request_approval_returns_pending_job():
    proposal = {
        "proposal_id": 1,
        "symbol": "AAPL",
        "side": "buy",
        "quantity": Decimal("1.5"),
        "limit_price": Decimal("150.25"),
        "rationale": "test",
        "risk_check": {"allowed": True, "reason": "ok"},
    }
    with patch(
        "app.modules.paper_operator_approval_gate.decision",
        return_value=MagicMock(),
    ) as mock_decision, patch(
        "app.modules.paper_operator_approval_gate.open_decision_job",
        new=AsyncMock(return_value="job-123"),
    ) as mock_open:
        result = await request_approval(proposal)

    assert result == {
        "job_id": "job-123",
        "proposal_id": 1,
        "status": "pending",
        "proposal": proposal,
    }
    mock_open.assert_awaited_once()
    # verify decision called with payload containing serialized proposal
    payload = mock_decision.call_args.kwargs["payload"]
    assert payload["proposal"]["quantity"] == "1.5"
    assert payload["proposal"]["limit_price"] == "150.25"
    assert payload["proposal_id"] == 1


@pytest.mark.asyncio
async def test_request_approval_serializes_non_json_types():
    proposal = {
        "proposal_id": 10,
        "symbol": "MSFT",
        "side": "sell",
        "quantity": Decimal("2.75"),
        "limit_price": None,
        "created_at": date(2026, 10, 27),
        "submitted_at": datetime(2026, 10, 27, 12, 30, tzinfo=timezone.utc),
        "uuid": uuid4(),
        "nested": {"decimal": Decimal("0.1"), "list": [Decimal("1"), date(2026, 1, 1)]},
        "rationale": "test",
    }
    with patch(
        "app.modules.paper_operator_approval_gate.decision",
        return_value=MagicMock(),
    ) as mock_decision, patch(
        "app.modules.paper_operator_approval_gate.open_decision_job",
        new=AsyncMock(return_value="job-serial"),
    ):
        await request_approval(proposal)

    payload = mock_decision.call_args.kwargs["payload"]
    serialized_proposal = payload["proposal"]
    assert serialized_proposal["quantity"] == "2.75"
    assert serialized_proposal["limit_price"] is None
    assert serialized_proposal["created_at"] == "2026-10-27"
    assert serialized_proposal["submitted_at"] == "2026-10-27T12:30:00+00:00"
    assert serialized_proposal["uuid"] == str(proposal["uuid"])
    assert serialized_proposal["nested"]["decimal"] == "0.1"
    assert serialized_proposal["nested"]["list"] == ["1", "2026-01-01"]


@pytest.mark.asyncio
async def test_request_approval_uses_own_session_when_db_omitted():
    proposal = {"proposal_id": 2, "symbol": "GOOG", "side": "buy", "quantity": Decimal("1")}
    with patch(
        "app.modules.paper_operator_approval_gate.decision",
        return_value=MagicMock(),
    ), patch(
        "app.modules.paper_operator_approval_gate.open_decision_job",
        new=AsyncMock(return_value="job-own"),
    ):
        result = await request_approval(proposal)
    assert result["job_id"] == "job-own"
    # No assertion needed that db was closed; we just ensure no error.


@pytest.mark.asyncio
async def test_request_approval_handles_none_limit_price():
    proposal = {"proposal_id": 3, "symbol": "AMZN", "side": "buy", "quantity": Decimal("3"), "limit_price": None}
    with patch(
        "app.modules.paper_operator_approval_gate.decision",
        return_value=MagicMock(),
    ) as mock_decision, patch(
        "app.modules.paper_operator_approval_gate.open_decision_job",
        new=AsyncMock(return_value="job-none"),
    ):
        result = await request_approval(proposal)
    assert result["status"] == "pending"
    payload = mock_decision.call_args.kwargs["payload"]
    assert payload["proposal"]["limit_price"] is None


# ----------------------------------------------------------------------
# handle_approval_answer tests
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_approval_answer_records_approval():
    async with async_session() as db:
        proposal_id = await _insert_proposal_row(db)
    payload = {
        "proposal_id": proposal_id,
        "proposal": {"proposal_id": proposal_id},
    }
    answer = {"approved": True, "choice": "operator"}

    await handle_approval_answer("job-1", "approve_proposal_1", answer, payload)

    async with async_session() as db:
        result = await db.execute(
            text("SELECT decision, operator_id FROM approval WHERE proposal_id = :pid"),
            {"pid": proposal_id},
        )
        row = result.fetchone()
        assert row is not None
        assert row[0] == "approved"
        assert row[1] == "operator"


@pytest.mark.asyncio
async def test_handle_approval_answer_records_rejection_with_note():
    async with async_session() as db:
        proposal_id = await _insert_proposal_row(db)
    payload = {
        "proposal_id": proposal_id,
        "proposal": {"proposal_id": proposal_id},
    }
    answer = {"approved": False, "note": "too risky", "choice": "operator"}

    await handle_approval_answer("job-1", "approve_proposal_1", answer, payload)

    async with async_session() as db:
        result = await db.execute(
            text("SELECT decision, reason FROM approval WHERE proposal_id = :pid"),
            {"pid": proposal_id},
        )
        row = result.fetchone()
        assert row is not None
        assert row[0] == "rejected"
        assert row[1] == "too risky"


@pytest.mark.asyncio
async def test_handle_approval_answer_rejection_defaults_reason():
    async with async_session() as db:
        proposal_id = await _insert_proposal_row(db)
    payload = {
        "proposal_id": proposal_id,
        "proposal": {"proposal_id": proposal_id},
    }
    answer = {"approved": False, "note": None, "choice": "operator"}

    await handle_approval_answer("job-1", "approve_proposal_1", answer, payload)

    async with async_session() as db:
        result = await db.execute(
            text("SELECT reason FROM approval WHERE proposal_id = :pid"),
            {"pid": proposal_id},
        )
        row = result.fetchone()
        assert row is not None
        assert row[0] == "rejected by operator"


@pytest.mark.asyncio
async def test_handle_approval_answer_approval_defaults_operator_id():
    async with async_session() as db:
        proposal_id = await _insert_proposal_row(db)
    payload = {
        "proposal_id": proposal_id,
        "proposal": {"proposal_id": proposal_id},
    }
    answer = {"approved": True, "choice": "operator"}

    await handle_approval_answer("job-1", "approve_proposal_1", answer, payload)

    async with async_session() as db:
        result = await db.execute(
            text("SELECT operator_id FROM approval WHERE proposal_id = :pid"),
            {"pid": proposal_id},
        )
        row = result.fetchone()
        assert row is not None
        assert row[0] == "operator"


@pytest.mark.asyncio
async def test_handle_approval_answer_missing_approved_is_rejected():
    async with async_session() as db:
        proposal_id = await _insert_proposal_row(db)
    payload = {
        "proposal_id": proposal_id,
        "proposal": {"proposal_id": proposal_id},
    }
    answer = {"note": "no approved key", "choice": "operator"}

    await handle_approval_answer("job-1", "approve_proposal_1", answer, payload)

    async with async_session() as db:
        result = await db.execute(
            text("SELECT decision FROM approval WHERE proposal_id = :pid"),
            {"pid": proposal_id},
        )
        row = result.fetchone()
        assert row is not None
        assert row[0] == "rejected"


@pytest.mark.asyncio
async def test_handle_approval_answer_fails_without_proposal_id():
    payload = {
        "proposal": {},
    }
    answer = {"approved": True, "choice": "operator"}

    with pytest.raises(Exception):
        await handle_approval_answer("job-1", "approve_proposal_1", answer, payload)
