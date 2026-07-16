"""Ledger-backed equity reconstruction.

Equity(t) = cash(t) + Σ position_qty(t) · mark(t). The ledger gives cash(t)
exactly (`balance_after` is self-verifying), and positions are replayed from
the trades attached to ledger entries. Between trades each asset is marked at
its most recent FILL price — the price the portfolio actually transacted at —
rather than re-querying bars per timestamp, which keeps reconstruction
O(entries) on a single query. A final "now" point marks open positions at the
latest market close, so the curve ends at the same equity number the
leaderboard ranks on.

TODO: this replays the full ledger on every request, and the frontend now polls
it every 30s per viewer. Fine at demo scale; once portfolios accumulate real
history, cache the replayed curve and extend it from the last snapshot instead.
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import LedgerEntry, Trade
from app.models.enums import OrderSide
from app.services.pricing import latest_price

_CENTS = Decimal("0.01")


@dataclass(frozen=True)
class EquityPoint:
    time: datetime
    cash: Decimal
    equity: Decimal


def equity_histories(
    db: Session, portfolio_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[EquityPoint]]:
    """One equity curve per portfolio, in ledger order, batched in one query
    so the leaderboard can sparkline N portfolios without N round-trips."""
    rows = db.execute(
        select(
            LedgerEntry.portfolio_id, LedgerEntry.created_at, LedgerEntry.balance_after,
            Trade.asset_id, Trade.side, Trade.qty, Trade.fill_price,
        )
        .join(Trade, Trade.id == LedgerEntry.trade_id, isouter=True)
        .where(LedgerEntry.portfolio_id.in_(portfolio_ids))
        .order_by(LedgerEntry.portfolio_id, LedgerEntry.created_at, LedgerEntry.id)
    ).all()

    curves: dict[uuid.UUID, list[EquityPoint]] = {pid: [] for pid in portfolio_ids}
    qtys: dict[uuid.UUID, dict[int, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    marks: dict[uuid.UUID, dict[int, Decimal]] = defaultdict(dict)

    # Fill prices and closes are in each ASSET's quote currency; the ledger is
    # USD — marks divide by the same per-asset FX factor the SQL valuations use.
    from app.services.valuation import usd_factors
    _fx = usd_factors(db, {r.asset_id for r in rows if r.asset_id is not None})

    for r in rows:
        if r.asset_id is not None:  # trade-backed entry: update replayed position
            signed = r.qty if r.side == OrderSide.BUY else -r.qty
            qtys[r.portfolio_id][r.asset_id] += signed
            marks[r.portfolio_id][r.asset_id] = r.fill_price
        pos_value = sum(
            (q * marks[r.portfolio_id][a] / _fx.get(a, Decimal("1"))
             for a, q in qtys[r.portfolio_id].items() if q),
            Decimal("0"),
        )
        curves[r.portfolio_id].append(EquityPoint(
            time=r.created_at,
            cash=r.balance_after,
            equity=(r.balance_after + pos_value).quantize(_CENTS, rounding=ROUND_HALF_UP),
        ))

    # Terminal point: open positions marked to the latest market close (falls
    # back to the last fill if an asset has no bars). Skipped for flat books —
    # their equity is already exact at the last ledger entry.
    now = datetime.now(timezone.utc)
    for pid, points in curves.items():
        held = {a: q for a, q in qtys[pid].items() if q}
        if not points or not held:
            continue
        pos_value = sum(
            (q * (latest_price(db, a) or marks[pid][a]) / _fx.get(a, Decimal("1"))
             for a, q in held.items()),
            Decimal("0"),
        )
        cash = points[-1].cash
        points.append(EquityPoint(
            time=now, cash=cash,
            equity=(cash + pos_value).quantize(_CENTS, rounding=ROUND_HALF_UP),
        ))
    return curves
