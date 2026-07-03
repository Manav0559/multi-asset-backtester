"""Head-to-head competition scoring.

The consent contract lives here: `windowed_metrics` returns ONLY aggregates
derived from a portfolio's equity curve — return, drawdown, a windowed Sharpe,
trade count, and an equity-step win rate — plus a normalized curve. It never
exposes positions, individual trades, orders, or strategy code. Both the live
comparison endpoint and the finish job call this same function, so what a
participant sees live is exactly what gets frozen at the end.
"""
from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Challenge, Trade
from app.models.enums import ChallengeStatus
from app.services.equity import equity_histories

_CENTS = Decimal("0.01")
_SPARK = 40


def current_equity(db: Session, portfolio_id: uuid.UUID) -> Decimal:
    """Equity right now = the terminal point of the ledger-replayed curve."""
    points = equity_histories(db, [portfolio_id]).get(portfolio_id, [])
    return points[-1].equity if points else Decimal("0.00")


def _downsample(seq: list, n: int) -> list:
    if len(seq) <= n:
        return seq
    step = (len(seq) - 1) / (n - 1)
    return [seq[round(i * step)] for i in range(n)]


def windowed_metrics(db: Session, portfolio_id: uuid.UUID,
                     baseline: Decimal, start_at: datetime) -> dict:
    """Consent-safe aggregates for [start_at, now]. Curve normalized to 100 at
    the baseline. No position/trade/strategy detail is ever included.

    The window ends at `now`, not the challenge's end_at: equity is replayed and
    marked to the latest close (we don't keep point-in-time equity at end_at),
    and the finish job runs within a minute of end_at, so `now`≈`end_at`. At
    finish these values are frozen onto the row, so they stop moving even as the
    portfolios keep trading."""
    baseline = Decimal(baseline)
    # Lower-bound filter only: equity_histories never emits future points (its
    # terminal "now" mark is the current equity), so no upper bound is needed —
    # and capturing one races that terminal timestamp.
    points = [p for p in equity_histories(db, [portfolio_id]).get(portfolio_id, [])
              if p.time >= start_at]
    # Always anchor the series at 100 at start_at so a quiet window still plots.
    norm = [100.0]
    for p in points:
        norm.append(float(p.equity / baseline * 100) if baseline else 100.0)

    end_equity = points[-1].equity if points else baseline
    return_pct = float((end_equity - baseline) / baseline * 100) if baseline else 0.0

    # Max drawdown over the normalized series (peak-to-trough).
    peak, max_dd = norm[0], 0.0
    for v in norm:
        peak = max(peak, v)
        max_dd = max(max_dd, (peak - v) / peak if peak else 0.0)

    # Windowed Sharpe + win rate from per-step returns (documented approximation:
    # points are irregular ledger events, so this is un-annualized).
    steps = [(norm[i] - norm[i - 1]) / norm[i - 1] for i in range(1, len(norm))
             if norm[i - 1]]
    if len(steps) >= 2:
        mean = sum(steps) / len(steps)
        var = sum((s - mean) ** 2 for s in steps) / (len(steps) - 1)
        std = math.sqrt(var)
        sharpe = mean / std if std else 0.0
        win_rate = sum(1 for s in steps if s > 0) / len(steps) * 100
    else:
        sharpe, win_rate = 0.0, 0.0

    n_trades = db.scalar(
        select(func.count()).select_from(Trade).where(
            Trade.portfolio_id == portfolio_id,
            Trade.executed_at >= start_at)) or 0

    times = [start_at.isoformat()] + [p.time.isoformat() for p in points]
    curve = _downsample(list(zip(times, [round(v, 4) for v in norm])), _SPARK)

    return {
        "return_pct": round(return_pct, 4),
        "max_drawdown_pct": round(max_dd * 100, 4),
        "sharpe": round(sharpe, 4),
        "win_rate": round(win_rate, 2),
        "n_trades": int(n_trades),
        "equity": str(end_equity.quantize(_CENTS, rounding=ROUND_HALF_UP)),
        "curve": [{"t": t, "v": v} for t, v in curve],
    }


def decide_winner(challenge: Challenge, ch_m: dict, op_m: dict) -> uuid.UUID | None:
    """Higher return wins; ties broken by higher windowed Sharpe; still tied ⇒
    draw (None)."""
    if ch_m["return_pct"] != op_m["return_pct"]:
        return (challenge.challenger_id if ch_m["return_pct"] > op_m["return_pct"]
                else challenge.opponent_id)
    if ch_m["sharpe"] != op_m["sharpe"]:
        return (challenge.challenger_id if ch_m["sharpe"] > op_m["sharpe"]
                else challenge.opponent_id)
    return None


def finish_challenge(db: Session, challenge: Challenge) -> None:
    """Freeze final metrics + winner. Idempotent: a finished row is left alone."""
    if challenge.status != ChallengeStatus.ACTIVE:
        return
    ch_m = windowed_metrics(db, challenge.challenger_portfolio_id,
                            challenge.challenger_baseline, challenge.start_at)
    op_m = windowed_metrics(db, challenge.opponent_portfolio_id,
                            challenge.opponent_baseline, challenge.start_at)
    challenge.winner_id = decide_winner(challenge, ch_m, op_m)
    challenge.final_metrics = {"challenger": ch_m, "opponent": op_m,
                               "winner_id": str(challenge.winner_id) if challenge.winner_id else None}
    challenge.status = ChallengeStatus.FINISHED
    challenge.finished_at = datetime.now(timezone.utc)


def finish_expired(db: Session) -> int:
    """Close every ACTIVE challenge past its end_at. Returns count finished."""
    now = datetime.now(timezone.utc)
    due = db.execute(
        select(Challenge).where(Challenge.status == ChallengeStatus.ACTIVE,
                                Challenge.end_at <= now)
    ).scalars().all()
    for ch in due:
        finish_challenge(db, ch)
    db.commit()
    return len(due)
