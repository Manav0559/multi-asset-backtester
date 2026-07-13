"""Shared USD position-valuation SQL — the ONE definition of "what are the
open positions worth in the portfolio's base currency".

Marks each position at its asset's latest stored close (falling back to the
position's average entry — a stale-but-honest mark beats a zero), then
converts the asset's quote currency to USD through fx_rates
(usd_value = ccy_value / rate(USDccy)). Used by BOTH the snapshot beat and
the leaderboard ranking so their equity numbers can never disagree with each
other. services/equity.py applies the same convention python-side for ledger
replay (see `usd_factors`).

A non-USD asset with no fx row values as NULL and drops out of the SUM — in
practice unreachable, because execution rejects non-USD fills until a rate
exists and rates are never deleted.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models import Asset, FxRate, OhlcvBar, Portfolio
from app.models.trading import Position


def usd_positions_value_subquery():
    """Correlated scalar subquery: Σ qty · mark / fx for Portfolio.id."""
    latest_close = (
        select(OhlcvBar.close)
        .where(OhlcvBar.asset_id == Position.asset_id)
        .order_by(OhlcvBar.time.desc())
        .limit(1)
        .correlate(Position)
        .scalar_subquery()
    )
    fx = (
        select(FxRate.rate)
        .where(FxRate.pair == func.concat("USD", Asset.currency))
        .scalar_subquery()
    )
    usd_divisor = case((Asset.currency == "USD", 1), else_=fx)
    return (
        select(func.coalesce(func.sum(
            Position.qty
            * func.coalesce(latest_close, Position.avg_entry_price)
            / usd_divisor), 0))
        .select_from(Position)
        .join(Asset, Asset.id == Position.asset_id)
        .where(Position.portfolio_id == Portfolio.id, Position.qty != 0)
        .correlate(Portfolio)
        .scalar_subquery()
    )


def usd_factors(db: Session, asset_ids: set[int]) -> dict[int, Decimal]:
    """Python-side twin for ledger replay: asset_id -> divisor (1 for USD,
    the USDccy rate otherwise; missing rate -> 1 with the same 'unreachable
    in practice' caveat as the SQL — better a labeled approximation than a
    crash in a read path)."""
    if not asset_ids:
        return {}
    rows = db.execute(
        select(Asset.id, Asset.currency).where(Asset.id.in_(asset_ids))).all()
    rates = {p: Decimal(r) for p, r in db.execute(select(FxRate.pair, FxRate.rate))}
    out: dict[int, Decimal] = {}
    for aid, ccy in rows:
        out[aid] = (Decimal("1") if ccy == "USD"
                    else rates.get(f"USD{ccy}", Decimal("1")))
    return out
