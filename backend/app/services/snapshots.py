"""Periodic equity snapshots — the data source for windowed leaderboards.

One row per portfolio per beat tick: equity = cash + Σ qty · mark, where the
mark is the asset's latest close (same convention as the terminal point in
services/equity.py) and falls back to the position's entry price when an
asset has no bars yet — a stale-but-honest mark beats poisoning the row to 0.
All portfolios are snapshotted, not just public ones, so flipping a portfolio
public later doesn't start its window history from scratch.

The whole snapshot is ONE `INSERT ... FROM SELECT` statement. That's not just
fewer round-trips: a read-then-insert version raced portfolio deletion (the
FK violation fired in the wild when a portfolio vanished between the SELECT
and the INSERT). Computing and inserting in a single statement makes the FK
impossible to violate.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, insert, literal, select
from sqlalchemy.orm import Session

from app.models import OhlcvBar, Portfolio, PortfolioEquitySnapshot
from app.models.trading import Position


def snapshot_portfolio_equity(db: Session, at: datetime | None = None) -> int:
    """Append one equity snapshot per portfolio. Returns rows written."""
    now = at or datetime.now(timezone.utc)

    latest_close = (
        select(OhlcvBar.close)
        .where(OhlcvBar.asset_id == Position.asset_id)
        .order_by(OhlcvBar.time.desc())
        .limit(1)
        .correlate(Position)
        .scalar_subquery()
    )
    positions_value = (
        select(func.coalesce(
            func.sum(Position.qty * func.coalesce(latest_close, Position.avg_entry_price)), 0))
        .where(Position.portfolio_id == Portfolio.id, Position.qty != 0)
        .correlate(Portfolio)
        .scalar_subquery()
    )
    result = db.execute(
        insert(PortfolioEquitySnapshot).from_select(
            ["portfolio_id", "time", "cash", "equity"],
            select(
                Portfolio.id,
                literal(now),
                Portfolio.cash_balance,
                # Numeric(20,2) column scale rounds the mark to cents on insert.
                Portfolio.cash_balance + positions_value,
            ),
        )
    )
    db.commit()
    return result.rowcount or 0
