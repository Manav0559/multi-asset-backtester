"""Vectorized backtest engine.

Given an OHLCV DataFrame and a Strategy, computes the strategy's return
stream, equity curve, and trade list with numpy/pandas vector ops — no
Python loop over bars.

No-lookahead guarantee (the critical quant-correctness point):
  position_t = signal_{t-1}
The signal computed from data up to and including bar t only takes effect on
bar t+1's return. So a strategy can never trade on information it wouldn't
have had in real time. This single `.shift(1)` is what separates an honest
backtest from a leaked one.

Costs: commission (bps) is charged on turnover = |position change|, so both
entries and exits pay. Fills use close-to-close returns (v1); a bid/ask +
market-impact model is a Phase-2 extension.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from app.backtest.costs import CostModel
from app.backtest.strategies import Strategy


@dataclass
class BacktestOutput:
    returns: pd.Series          # per-bar strategy returns (after costs)
    equity: pd.Series           # equity curve (starts at initial_capital)
    position: pd.Series         # realized position per bar (post-shift)
    trades: pd.DataFrame        # one row per position change
    initial_capital: float


def run_backtest(
    df: pd.DataFrame,
    strategy: Strategy,
    *,
    initial_capital: float = 100_000.0,
    commission_bps: float = 0.0,
    cost_model: CostModel | None = None,
) -> BacktestOutput:
    if df.empty:
        raise ValueError("no price data to backtest")
    df = df.sort_index()
    close = df["close"].astype(float)
    volume = df["volume"].astype(float) if "volume" in df else pd.Series(0.0, index=close.index)

    target = strategy(df).astype(float).reindex(close.index).fillna(0.0)

    # NO-LOOKAHEAD: act on the prior bar's signal.
    position = target.shift(1).fillna(0.0)

    bar_returns = close.pct_change().fillna(0.0)
    gross = position * bar_returns

    # Costs on turnover. A bare commission_bps is sugar for a linear CostModel;
    # pass a full cost_model for spread + square-root market impact.
    model = cost_model or CostModel(commission_bps=commission_bps)
    cost = model.per_bar_cost(position, close, volume, initial_capital)
    net = gross - cost

    equity = (1.0 + net).cumprod() * initial_capital

    trades = _extract_trades(position, close)

    return BacktestOutput(returns=net, equity=equity, position=position,
                          trades=trades, initial_capital=initial_capital)


def _extract_trades(position: pd.Series, close: pd.Series) -> pd.DataFrame:
    """Pair entries and exits into round-trip trades with per-trade return."""
    changes = position.diff().fillna(position)
    entries, records = [], []
    for ts, chg in changes[changes != 0].items():
        if chg > 0:  # opening / increasing long
            entries.append((ts, close.loc[ts]))
        elif chg < 0 and entries:  # closing
            entry_ts, entry_px = entries.pop(0)
            exit_px = close.loc[ts]
            records.append({
                "entry_time": entry_ts, "exit_time": ts,
                "entry_price": entry_px, "exit_price": exit_px,
                "return_pct": (exit_px / entry_px - 1.0) * 100.0,
            })
    return pd.DataFrame(records, columns=["entry_time", "exit_time", "entry_price",
                                          "exit_price", "return_pct"])
