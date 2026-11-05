# app/modules/paper_operator_view.py
"""Operator view for the paper trading pipeline.

Reads from the audit trail (proposal, approval, fill, pnl) and returns a
consolidated view: pending proposals first, then equity curve vs SPY,
open positions, and performance metrics. No live trading logic; no
network or secrets access.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import text

from app.database import async_session

logger = logging.getLogger("scaffold")


async def _get_view(db: Any) -> dict[str, Any]:
    """Run all queries against the given session and build the view."""

    # Pending proposals: proposals with no approval row yet.
    pending_query = text(
        """
        SELECT p.id, p.ticker, p.side, p.quantity, p.limit_price,
               p.order_type, p.rationale, p.signal_id, p.data_ts, p.model_ts
        FROM proposal p
        WHERE NOT EXISTS (
            SELECT 1 FROM approval a WHERE a.proposal_id = p.id
        )
        ORDER BY p.id
        """
    )
    pending_rows = (await db.execute(pending_query)).fetchall()
    pending_proposals = [dict(row._mapping) for row in pending_rows]

    # Open positions: net quantity per ticker from fills, joined to proposal.
    positions_query = text(
        """
        SELECT p.ticker,
               COALESCE(SUM(CASE WHEN f.side = 'buy' THEN f.quantity
                                 ELSE -f.quantity END), 0) AS net_qty
        FROM fill f
        JOIN broker_order_id b ON f.broker_order_id = b.id
        JOIN proposal p ON b.proposal_id = p.id
        GROUP BY p.ticker
        HAVING COALESCE(SUM(CASE WHEN f.side = 'buy' THEN f.quantity
                                 ELSE -f.quantity END), 0) > 0
        """
    )
    positions_rows = (await db.execute(positions_query)).fetchall()
    open_positions = [dict(row._mapping) for row in positions_rows]

    # Equity curve vs SPY from pnl rows.
    equity_query = text(
        """
        SELECT as_of_ts, equity, spy_benchmark_return
        FROM pnl
        ORDER BY as_of_ts ASC
        """
    )
    equity_rows = (await db.execute(equity_query)).fetchall()
    equity_curve = [dict(row._mapping) for row in equity_rows]

    # Latest metrics from the most recent pnl row.
    metrics_query = text(
        """
        SELECT cumulative_return, max_drawdown, sharpe_ratio, turnover
        FROM pnl
        ORDER BY as_of_ts DESC
        LIMIT 1
        """
    )
    metrics_row = (await db.execute(metrics_query)).fetchone()
    if metrics_row is not None:
        metrics = dict(metrics_row._mapping)
    else:
        metrics = {
            "cumulative_return": None,
            "max_drawdown": None,
            "sharpe_ratio": None,
            "turnover": None,
        }

    return {
        "pending_proposals": pending_proposals,
        "open_positions": open_positions,
        "equity_curve": equity_curve,
        "metrics": metrics,
    }


async def get_operator_view(*, db: Optional[Any] = None) -> dict[str, Any]:
    """Return a consolidated operator view from the audit trail.

    Args:
        db: Optional async SQLAlchemy session. If None, a new session is
            opened and closed internally.

    Returns:
        A dict with keys: ``pending_proposals`` (list of dicts),
        ``open_positions`` (list of dicts), ``equity_curve`` (list of
        dicts), and ``metrics`` (dict).
    """
    if db is not None:
        return await _get_view(db)
    async with async_session() as session:
        return await _get_view(session)
