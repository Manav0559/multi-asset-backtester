"""Leaderboard — the competitive layer over shared portfolios.

Ranks PUBLIC portfolios (is_public opt-in, set at creation) by return over a
selectable window (24h / 7d / all-time), where equity = cash + open positions
marked at each asset's latest close. Equity, return, and rank are computed in
ONE SQL statement — correlated scalar subqueries against ohlcv_bars ride the
(asset, timeframe, time DESC) btree, and rank() runs over ALL public
portfolios before LIMIT so a page never renumbers ranks. Positions with no
known price contribute 0 rather than poisoning the whole row to NULL.

Windowed returns use portfolio_equity_snapshots (written every few minutes by
the Celery beat task): the baseline is the latest snapshot at or before the
window start, falling back to initial_cash for portfolios younger than the
window — their whole life fits inside it, so all-time IS their windowed return.
"""
import time as time_mod
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.metrics import LEADERBOARD_QUERY_TIME
from app.db.session import get_db
from app.models import OhlcvBar, Portfolio, PortfolioEquitySnapshot, PortfolioMember, User
from app.models.trading import Position
from app.schemas.leaderboard import LeaderboardEntryOut
from app.services.equity import equity_histories

router = APIRouter(prefix="/leaderboard", tags=["leaderboard"])

_CENTS = Decimal("0.01")
_PCT = Decimal("0.0001")
_SPARK_POINTS = 30

_WINDOWS: dict[str, timedelta] = {"24h": timedelta(hours=24), "7d": timedelta(days=7)}


def _downsample(values: list[Decimal], n: int) -> list[Decimal]:
    """Thin a series to <= n points, always keeping the endpoints."""
    if len(values) <= n:
        return values
    step = (len(values) - 1) / (n - 1)
    return [values[round(i * step)] for i in range(n)]


@router.get("", response_model=list[LeaderboardEntryOut])
def get_leaderboard(
    limit: int = Query(default=20, ge=1, le=100),
    window: Literal["24h", "7d", "all"] = Query(default="all"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[LeaderboardEntryOut]:
    query_start = time_mod.perf_counter()
    latest_close = (
        select(OhlcvBar.close)
        .where(OhlcvBar.asset_id == Position.asset_id)
        .order_by(OhlcvBar.time.desc())
        .limit(1)
        .correlate(Position)
        .scalar_subquery()
    )
    positions_value = (
        select(func.coalesce(func.sum(Position.qty * func.coalesce(latest_close, 0)), 0))
        .where(Position.portfolio_id == Portfolio.id, Position.qty != 0)
        .correlate(Portfolio)
        .scalar_subquery()
    )
    equity = Portfolio.cash_balance + positions_value

    if window == "all":
        # initial_cash > 0 is enforced at creation, so the division is safe.
        baseline = Portfolio.initial_cash
    else:
        cutoff = datetime.now(timezone.utc) - _WINDOWS[window]
        baseline_snapshot = (
            select(PortfolioEquitySnapshot.equity)
            .where(
                PortfolioEquitySnapshot.portfolio_id == Portfolio.id,
                PortfolioEquitySnapshot.time <= cutoff,
            )
            .order_by(PortfolioEquitySnapshot.time.desc())
            .limit(1)
            .correlate(Portfolio)
            .scalar_subquery()
        )
        baseline = func.coalesce(baseline_snapshot, Portfolio.initial_cash)
    # nullif guards a (legitimately) zero snapshot baseline — a wiped-out
    # portfolio ranks at 0% rather than erroring the whole board.
    return_pct = func.coalesce((equity - baseline) / func.nullif(baseline, 0) * 100, 0)

    rows = db.execute(
        select(
            Portfolio.id,
            Portfolio.name,
            Portfolio.initial_cash,
            equity.label("equity"),
            return_pct.label("return_pct"),
            func.rank().over(order_by=return_pct.desc()).label("rank"),
        )
        .where(Portfolio.is_public.is_(True))
        .order_by(return_pct.desc(), Portfolio.created_at)
        .limit(limit)
    ).all()
    LEADERBOARD_QUERY_TIME.labels(window).observe(time_mod.perf_counter() - query_start)
    if not rows:
        return []

    ids = [r.id for r in rows]
    member_rows = db.execute(
        select(
            PortfolioMember.portfolio_id,
            func.array_agg(aggregate_order_by(User.username, PortfolioMember.joined_at)),
        )
        .join(User, User.id == PortfolioMember.user_id)
        .where(PortfolioMember.portfolio_id.in_(ids))
        .group_by(PortfolioMember.portfolio_id)
    ).all()
    members = {pid: names for pid, names in member_rows}
    sparks = equity_histories(db, ids)

    return [
        LeaderboardEntryOut(
            rank=r.rank,
            portfolio_id=r.id,
            name=r.name,
            members=members.get(r.id, []),
            initial_cash=r.initial_cash,
            equity=Decimal(r.equity).quantize(_CENTS, rounding=ROUND_HALF_UP),
            return_pct=Decimal(r.return_pct).quantize(_PCT, rounding=ROUND_HALF_UP),
            spark=_downsample([p.equity for p in sparks.get(r.id, [])], _SPARK_POINTS),
        )
        for r in rows
    ]
