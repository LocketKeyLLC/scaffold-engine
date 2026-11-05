# tests/test_paper_entry_approval.py
"""Tests for the paper trading lab approval entry point."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import text

from app.database import async_session
from app.modules.paper_entry_approval import run_approval_entry_point


def _make_signal(
    *,
    ticker: str = "AAPL",
    signal_type: str = "form4_purchase",
    direction: str = "long",
    rationale: str = "Insider purchase",
) -> dict:
    return {
        "id": 1,
        "ticker": ticker,
        "signal_type": signal_type,
        "direction": direction,
        "rationale": rationale,
    }


def _make_proposal(
    *,
    proposal_id: int = 1,
    ticker: str = "AAPL",
    side: str = "buy",
    quantity: int = 10,
    order_type: str = "market",
    limit_price: float | None = None,
    rationale: str = "Test rationale",
    equity: float = 100000.0,
    day_start_equity: float = 100000.0,
    orders_today: int = 0,
    proposed_notional: float = 1500.0,
    current_position_notional: float = 0.0,
) -> dict:
    return {
        "id": proposal_id,
        "ticker": ticker,
        "side": side,
        "quantity": quantity,
        "order_type": order_type,
        "limit_price": limit_price,
        "rationale": rationale,
        "equity": equity,
        "day_start_equity": day_start_equity,
        "orders_today": orders_today,
        "proposed_notional": proposed_notional,
        "current_position_notional": current_position_notional,
    }


@pytest.mark.asyncio
async def test_no_signals_returns_zero_counts():
    """No signals generated returns a summary with zero counts."""
    with patch(
        "app.modules.paper_entry_approval.generate_signals",
        new=AsyncMock(return_value=[]),
    ):
        result = await run_approval_entry_point()
    assert result == {"signals": 0, "proposals": 0, "approvals": 0, "results": []}


@pytest.mark.asyncio
async def test_no_proposals_returns_signal_count():
    """Signals generated but no proposals returns signal count."""
    signals = [_make_signal()]
    with patch(
        "app.modules.paper_entry_approval.generate_signals",
        new=AsyncMock(return_value=signals),
    ), patch(
        "app.modules.paper_entry_approval.propose_trades",
        new=AsyncMock(return_value=[]),
    ):
        result = await run_approval_entry_point()
    assert result == {"signals": 1, "proposals": 0, "approvals": 0, "results": []}


@pytest.mark.asyncio
async def test_risk_rejected_proposal_not_sent_for_approval():
    """A proposal failing risk checks is rejected without approval."""
    signals = [_make_signal()]
    proposals = [_make_proposal()]
    risk_decision = MagicMock()
    risk_decision.allowed = False
    risk_decision.reason = "Position size exceeds 5% limit"
    with patch(
        "app.modules.paper_entry_approval.generate_signals",
        new=AsyncMock(return_value=signals),
    ), patch(
        "app.modules.paper_entry_approval.propose_trades",
        new=AsyncMock(return_value=proposals),
    ), patch(
        "app.modules.paper_entry_approval.evaluate_proposal",
        return_value=risk_decision,
    ) as mock_eval, patch(
        "app.modules.paper_entry_approval.request_approval",
        new=AsyncMock(),
    ) as mock_request, patch(
        "app.modules.paper_entry_approval.write_approval",
        new=AsyncMock(),
    ) as mock_write:
        result = await run_approval_entry_point()

    assert result["approvals"] == 0
    assert result["results"][0]["decision"] == "rejected"
    assert result["results"][0]["reason"] == "Position size exceeds 5% limit"
    mock_request.assert_not_called()
    mock_write.assert_not_called()


@pytest.mark.asyncio
async def test_approved_proposal_written_to_audit_trail():
    """An approved proposal is written to the audit trail."""
    signals = [_make_signal()]
    proposals = [_make_proposal()]
    risk_decision = MagicMock()
    risk_decision.allowed = True
    risk_decision.reason = ""
    approval = {
        "decision": "approved",
        "reason": "Operator approved",
        "operator_id": "op-1",
    }
    with patch(
        "app.modules.paper_entry_approval.generate_signals",
        new=AsyncMock(return_value=signals),
    ), patch(
        "app.modules.paper_entry_approval.propose_trades",
        new=AsyncMock(return_value=proposals),
    ), patch(
        "app.modules.paper_entry_approval.evaluate_proposal",
        return_value=risk_decision,
    ), patch(
        "app.modules.paper_entry_approval.request_approval",
        new=AsyncMock(return_value=approval),
    ), patch(
        "app.modules.paper_entry_approval.write_approval",
        new=AsyncMock(),
    ) as mock_write:
        result = await run_approval_entry_point()

    assert result["approvals"] == 1
    assert result["results"][0]["decision"] == "approved"
    mock_write.assert_called_once_with(
        proposal_id=1,
        operator_id="op-1",
        decision="approved",
        reason="Operator approved",
    )


@pytest.mark.asyncio
async def test_rejected_approval_written_to_audit_trail():
    """A proposal the operator rejects is written as rejected."""
    signals = [_make_signal()]
    proposals = [_make_proposal()]
    risk_decision = MagicMock()
    risk_decision.allowed = True
    risk_decision.reason = ""
    approval = {
        "decision": "rejected",
        "reason": "Operator declined",
        "operator_id": "op-2",
    }
    with patch(
        "app.modules.paper_entry_approval.generate_signals",
        new=AsyncMock(return_value=signals),
    ), patch(
        "app.modules.paper_entry_approval.propose_trades",
        new=AsyncMock(return_value=proposals),
    ), patch(
        "app.modules.paper_entry_approval.evaluate_proposal",
        return_value=risk_decision,
    ), patch(
        "app.modules.paper_entry_approval.request_approval",
        new=AsyncMock(return_value=approval),
    ), patch(
        "app.modules.paper_entry_approval.write_approval",
        new=AsyncMock(),
    ) as mock_write:
        result = await run_approval_entry_point()

    assert result["approvals"] == 0
    assert result["results"][0]["decision"] == "rejected"
    mock_write.assert_called_once_with(
        proposal_id=1,
        operator_id="op-2",
        decision="rejected",
        reason="Operator declined",
    )


@pytest.mark.asyncio
async def test_multiple_proposals_mixed_decisions():
    """Multiple proposals with mixed risk and approval outcomes."""
    signals = [_make_signal(ticker="AAPL"), _make_signal(ticker="MSFT")]
    proposals = [
        _make_proposal(proposal_id=1, ticker="AAPL"),
        _make_proposal(proposal_id=2, ticker="MSFT"),
    ]
    risk_ok = MagicMock()
    risk_ok.allowed = True
    risk_ok.reason = ""
    risk_bad = MagicMock()
    risk_bad.allowed = False
    risk_bad.reason = "Daily loss limit reached"
    approval = {
        "decision": "approved",
        "reason": "OK",
        "operator_id": "op-3",
    }
    with patch(
        "app.modules.paper_entry_approval.generate_signals",
        new=AsyncMock(return_value=signals),
    ), patch(
        "app.modules.paper_entry_approval.propose_trades",
        new=AsyncMock(return_value=proposals),
    ), patch(
        "app.modules.paper_entry_approval.evaluate_proposal",
        side_effect=[risk_ok, risk_bad],
    ), patch(
        "app.modules.paper_entry_approval.request_approval",
        new=AsyncMock(return_value=approval),
    ), patch(
        "app.modules.paper_entry_approval.write_approval",
        new=AsyncMock(),
    ) as mock_write:
        result = await run_approval_entry_point()

    assert result["signals"] == 2
    assert result["proposals"] == 2
    assert result["approvals"] == 1
    assert result["results"][0]["decision"] == "approved"
    assert result["results"][1]["decision"] == "rejected"
    assert result["results"][1]["reason"] == "Daily loss limit reached"
    mock_write.assert_called_once()


@pytest.mark.asyncio
async def test_passes_timestamp_to_signal_generator():
    """The timestamp argument is forwarded to generate_signals."""
    ts = datetime(2026, 10, 23, 20, 30, tzinfo=timezone.utc)
    with patch(
        "app.modules.paper_entry_approval.generate_signals",
        new=AsyncMock(return_value=[]),
    ) as mock_gen:
        await run_approval_entry_point(timestamp=ts)
    mock_gen.assert_called_once_with(ts, db=mock_gen.await_args.kwargs["db"])


@pytest.mark.asyncio
async def test_generate_signals_exception_propagates():
    """An exception from generate_signals propagates to the caller."""
    with patch(
        "app.modules.paper_entry_approval.generate_signals",
        new=AsyncMock(side_effect=RuntimeError("signal failure")),
    ):
        with pytest.raises(RuntimeError, match="signal failure"):
            await run_approval_entry_point()


@pytest.mark.asyncio
async def test_propose_trades_exception_propagates():
    """An exception from propose_trades propagates to the caller."""
    signals = [_make_signal()]
    with patch(
        "app.modules.paper_entry_approval.generate_signals",
        new=AsyncMock(return_value=signals),
    ), patch(
        "app.modules.paper_entry_approval.propose_trades",
        new=AsyncMock(side_effect=RuntimeError("proposal failure")),
    ):
        with pytest.raises(RuntimeError, match="proposal failure"):
            await run_approval_entry_point()


@pytest.mark.asyncio
async def test_request_approval_exception_propagates():
    """An exception from request_approval propagates to the caller."""
    signals = [_make_signal()]
    proposals = [_make_proposal()]
    risk_decision = MagicMock()
    risk_decision.allowed = True
    risk_decision.reason = ""
    with patch(
        "app.modules.paper_entry_approval.generate_signals",
        new=AsyncMock(return_value=signals),
    ), patch(
        "app.modules.paper_entry_approval.propose_trades",
        new=AsyncMock(return_value=proposals),
    ), patch(
        "app.modules.paper_entry_approval.evaluate_proposal",
        return_value=risk_decision,
    ), patch(
        "app.modules.paper_entry_approval.request_approval",
        new=AsyncMock(side_effect=RuntimeError("approval failure")),
    ):
        with pytest.raises(RuntimeError, match="approval failure"):
            await run_approval_entry_point()


@pytest.mark.asyncio
async def test_write_approval_exception_propagates():
    """An exception from write_approval propagates to the caller."""
    signals = [_make_signal()]
    proposals = [_make_proposal()]
    risk_decision = MagicMock()
    risk_decision.allowed = True
    risk_decision.reason = ""
    approval = {
        "decision": "approved",
        "reason": "OK",
        "operator_id": "op-4",
    }
    with patch(
        "app.modules.paper_entry_approval.generate_signals",
        new=AsyncMock(return_value=signals),
    ), patch(
        "app.modules.paper_entry_approval.propose_trades",
        new=AsyncMock(return_value=proposals),
    ), patch(
        "app.modules.paper_entry_approval.evaluate_proposal",
        return_value=risk_decision,
    ), patch(
        "app.modules.paper_entry_approval.request_approval",
        new=AsyncMock(return_value=approval),
    ), patch(
        "app.modules.paper_entry_approval.write_approval",
        new=AsyncMock(side_effect=RuntimeError("write failure")),
    ):
        with pytest.raises(RuntimeError, match="write failure"):
            await run_approval_entry_point()


@pytest.mark.asyncio
async def test_uses_provided_db_session():
    """A caller-provided db session is used and not closed."""
    fake_db = MagicMock()
    fake_db.close = AsyncMock()
    with patch(
        "app.modules.paper_entry_approval.generate_signals",
        new=AsyncMock(return_value=[]),
    ) as mock_gen:
        await run_approval_entry_point(db=fake_db)
    mock_gen.assert_called_once()
    fake_db.close.assert_not_called()


@pytest.mark.asyncio
async def test_own_db_session_is_closed():
    """When no db is provided, the created session is closed."""
    fake_db = MagicMock()
    fake_db.close = AsyncMock()
    with patch(
        "app.modules.paper_entry_approval.async_session",
        return_value=fake_db,
    ), patch(
        "app.modules.paper_entry_approval.generate_signals",
        new=AsyncMock(return_value=[]),
    ):
        await run_approval_entry_point()
    fake_db.close.assert_called_once()
