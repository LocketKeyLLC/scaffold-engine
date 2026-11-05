# tests/test_paper_risk_control_module.py
"""Tests for the paper risk control module."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import text

from app.database import async_session
from app.modules.paper_risk_control_module import (
    ALLOW_LIST,
    MAX_DAILY_LOSS_PCT,
    MAX_ORDERS_PER_DAY,
    MAX_POSITION_PCT,
    RiskDecision,
    check_allow_list,
    check_daily_loss,
    check_order_count,
    check_position_size,
    evaluate_proposal,
    trigger_kill_switch,
)


# ---------------------------------------------------------------------------
# check_allow_list
# ---------------------------------------------------------------------------


def test_check_allow_list_valid_ticker():
    assert check_allow_list("AAPL").allowed is True


def test_check_allow_list_case_insensitive_and_whitespace():
    assert check_allow_list("  aapl ").allowed is True


def test_check_allow_list_not_on_list():
    decision = check_allow_list("ZZZZ")
    assert decision.allowed is False
    assert "not on the allow-list" in decision.reason


def test_check_allow_list_empty_ticker():
    decision = check_allow_list("")
    assert decision.allowed is False
    assert decision.reason == "empty ticker"


def test_check_allow_list_forbidden_option_pattern():
    decision = check_allow_list("AAPL-250117C00150000")
    assert decision.allowed is False
    assert "forbidden symbol pattern" in decision.reason


def test_check_allow_list_forbidden_crypto_pair():
    decision = check_allow_list("BTC/USD")
    assert decision.allowed is False
    assert "forbidden symbol pattern" in decision.reason


def test_check_allow_list_forbidden_crypto_symbol():
    decision = check_allow_list("BTC")
    assert decision.allowed is False
    assert "forbidden symbol pattern" in decision.reason


# ---------------------------------------------------------------------------
# check_position_size
# ---------------------------------------------------------------------------


def test_check_position_size_within_limit():
    equity = Decimal("10000")
    proposed = Decimal("400")  # 4% of equity
    decision = check_position_size(equity=equity, proposed_notional=proposed)
    assert decision.allowed is True


def test_check_position_size_exact_limit():
    equity = Decimal("10000")
    proposed = equity * MAX_POSITION_PCT  # exactly 5%
    decision = check_position_size(equity=equity, proposed_notional=proposed)
    assert decision.allowed is True


def test_check_position_size_exceeds_limit():
    equity = Decimal("10000")
    proposed = equity * MAX_POSITION_PCT + Decimal("0.01")
    decision = check_position_size(equity=equity, proposed_notional=proposed)
    assert decision.allowed is False
    assert "exceeds" in decision.reason


def test_check_position_size_with_existing_position():
    equity = Decimal("10000")
    current = Decimal("200")  # 2%
    proposed = Decimal("300")  # 3% -> total 5% (allowed)
    decision = check_position_size(
        equity=equity,
        proposed_notional=proposed,
        current_position_notional=current,
    )
    assert decision.allowed is True


def test_check_position_size_with_existing_position_exceeds():
    equity = Decimal("10000")
    current = Decimal("400")  # 4%
    proposed = Decimal("200")  # 2% -> total 6% (rejected)
    decision = check_position_size(
        equity=equity,
        proposed_notional=proposed,
        current_position_notional=current,
    )
    assert decision.allowed is False


def test_check_position_size_zero_equity():
    decision = check_position_size(equity=Decimal("0"), proposed_notional=Decimal("1"))
    assert decision.allowed is False
    assert decision.reason == "equity must be positive"


def test_check_position_size_negative_equity():
    decision = check_position_size(equity=Decimal("-1"), proposed_notional=Decimal("1"))
    assert decision.allowed is False
    assert decision.reason == "equity must be positive"


def test_check_position_size_negative_proposed_notional():
    decision = check_position_size(equity=Decimal("1000"), proposed_notional=Decimal("-1"))
    assert decision.allowed is False
    assert decision.reason == "proposed notional must be non-negative"


def test_check_position_size_negative_current_position():
    decision = check_position_size(
        equity=Decimal("1000"),
        proposed_notional=Decimal("1"),
        current_position_notional=Decimal("-1"),
    )
    assert decision.allowed is False
    assert decision.reason == "current position notional must be non-negative"


# ---------------------------------------------------------------------------
# check_daily_loss
# ---------------------------------------------------------------------------


def test_check_daily_loss_normal():
    decision = check_daily_loss(equity=Decimal("10100"), day_start_equity=Decimal("10000"))
    assert decision.allowed is True


def test_check_daily_loss_exact_threshold():
    equity = Decimal("10000") * (1 - MAX_DAILY_LOSS_PCT)
    decision = check_daily_loss(equity=equity, day_start_equity=Decimal("10000"))
    assert decision.allowed is False
    assert "halt threshold" in decision.reason


def test_check_daily_loss_below_threshold():
    equity = Decimal("10000") * (1 - MAX_DAILY_LOSS_PCT) - Decimal("0.01")
    decision = check_daily_loss(equity=equity, day_start_equity=Decimal("10000"))
    assert decision.allowed is False


def test_check_daily_loss_gain():
    decision = check_daily_loss(equity=Decimal("10300"), day_start_equity=Decimal("10000"))
    assert decision.allowed is True


def test_check_daily_loss_zero_day_start_equity():
    decision = check_daily_loss(equity=Decimal("10000"), day_start_equity=Decimal("0"))
    assert decision.allowed is False
    assert decision.reason == "day start equity must be positive"


def test_check_daily_loss_negative_day_start_equity():
    decision = check_daily_loss(equity=Decimal("10000"), day_start_equity=Decimal("-1"))
    assert decision.allowed is False
    assert decision.reason == "day start equity must be positive"


# ---------------------------------------------------------------------------
# check_order_count
# ---------------------------------------------------------------------------


def test_check_order_count_below_limit():
    decision = check_order_count(orders_today=2)
    assert decision.allowed is True


def test_check_order_count_at_limit():
    decision = check_order_count(orders_today=MAX_ORDERS_PER_DAY)
    assert decision.allowed is False
    assert "daily order limit reached" in decision.reason


def test_check_order_count_above_limit():
    decision = check_order_count(orders_today=MAX_ORDERS_PER_DAY + 1)
    assert decision.allowed is False


def test_check_order_count_negative():
    decision = check_order_count(orders_today=-1)
    assert decision.allowed is False
    assert decision.reason == "orders_today must be non-negative"


def test_check_order_count_custom_max():
    decision = check_order_count(orders_today=5, max_orders=5)
    assert decision.allowed is False
    decision = check_order_count(orders_today=4, max_orders=5)
    assert decision.allowed is True


# ---------------------------------------------------------------------------
# evaluate_proposal
# ---------------------------------------------------------------------------


def test_evaluate_proposal_all_pass():
    decision = evaluate_proposal(
        ticker="AAPL",
        equity=Decimal("10000"),
        day_start_equity=Decimal("10000"),
        orders_today=0,
        proposed_notional=Decimal("400"),
    )
    assert decision.allowed is True


def test_evaluate_proposal_rejects_bad_ticker_first():
    decision = evaluate_proposal(
        ticker="ZZZZ",
        equity=Decimal("10000"),
        day_start_equity=Decimal("10000"),
        orders_today=0,
        proposed_notional=Decimal("400"),
    )
    assert decision.allowed is False
    assert "not on the allow-list" in decision.reason


def test_evaluate_proposal_rejects_size_second():
    decision = evaluate_proposal(
        ticker="AAPL",
        equity=Decimal("10000"),
        day_start_equity=Decimal("10000"),
        orders_today=0,
        proposed_notional=Decimal("600"),  # 6% > 5%
    )
    assert decision.allowed is False
    assert "exceeds" in decision.reason


def test_evaluate_proposal_rejects_daily_loss_third():
    equity = Decimal("10000") * (1 - MAX_DAILY_LOSS_PCT) - Decimal("0.01")
    decision = evaluate_proposal(
        ticker="AAPL",
        equity=equity,
        day_start_equity=Decimal("10000"),
        orders_today=0,
        proposed_notional=Decimal("400"),
    )
    assert decision.allowed is False
    assert "halt threshold" in decision.reason


def test_evaluate_proposal_rejects_order_count_fourth():
    decision = evaluate_proposal(
        ticker="AAPL",
        equity=Decimal("10000"),
        day_start_equity=Decimal("10000"),
        orders_today=MAX_ORDERS_PER_DAY,
        proposed_notional=Decimal("400"),
    )
    assert decision.allowed is False
    assert "daily order limit reached" in decision.reason


# ---------------------------------------------------------------------------
# trigger_kill_switch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trigger_kill_switch_calls_both_functions():
    cancel_fn = AsyncMock()
    close_fn = AsyncMock()
    db = MagicMock()  # db not used by function, but required parameter

    await trigger_kill_switch(
        db=db,
        reason="test",
        cancel_orders_fn=cancel_fn,
        close_positions_fn=close_fn,
    )

    cancel_fn.assert_awaited_once()
    close_fn.assert_awaited_once()


@pytest.mark.asyncio
async def test_trigger_kill_switch_skips_none_functions():
    db = MagicMock()
    await trigger_kill_switch(db=db, reason="test")
    # No exception raised, nothing called


@pytest.mark.asyncio
async def test_trigger_kill_switch_one_function_fails_does_not_raise():
    cancel_fn = AsyncMock(side_effect=Exception("boom"))
    close_fn = AsyncMock()
    db = MagicMock()

    # Should not raise, logs exception
    await trigger_kill_switch(
        db=db,
        reason="test",
        cancel_orders_fn=cancel_fn,
        close_positions_fn=close_fn,
    )

    cancel_fn.assert_awaited_once()
    close_fn.assert_awaited_once()


@pytest.mark.asyncio
async def test_trigger_kill_switch_with_real_db_session():
    """Use async_session to ensure the function works with a real DB session."""
    async with async_session() as db:
        # Insert a dummy row to prove we are using the real scratch DB.
        await db.execute(
            text(
                "INSERT INTO ingested_item "
                "(source, external_id, published_ts, payload) "
                "VALUES ('news', :ext_id, now(), '{}'::json)"
            ),
            {"ext_id": "test-risk-module"},
        )
        await db.commit()

        cancel_fn = AsyncMock()
        close_fn = AsyncMock()
        await trigger_kill_switch(
            db=db,
            reason="test with real db",
            cancel_orders_fn=cancel_fn,
            close_positions_fn=close_fn,
        )
        cancel_fn.assert_awaited_once()
        close_fn.assert_awaited_once()
