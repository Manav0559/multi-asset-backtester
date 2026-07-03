"""Long/short, multi-asset portfolio backtest engine.

This generalizes the single-asset long/flat engine to the industry-standard
contract: a strategy produces TARGET PORTFOLIO WEIGHTS per asset per bar.

  weight[t, asset]  in  [-max .. +max]     (fraction of equity)
    > 0  long        < 0  short        0  flat
  sum(|weights|)  = gross leverage
  sum( weights )  = net exposure   (== 0 is dollar-neutral / market-neutral)

Everything real-world hangs off this contract without changing its shape:
  * shorting        -> negative weights (P&L flips sign; borrow cost applies)
  * multi-asset     -> multiple columns
  * position sizing -> weight magnitude
  * leverage cap    -> gross-exposure normalization
  * pairs / market-neutral -> weights summing to zero

No-lookahead is preserved exactly as before: realized weights are the
PRIOR bar's target (`.shift(1)`), so a strategy never trades on information
it wouldn't have had. Costs are charged per-asset on turnover; short
positions additionally accrue a borrow fee.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from app.backtest.costs import CostModel
from app.backtest.metrics import infer_periods_per_year

# A weight strategy takes a wide close-price frame (index=time, cols=assets)
# and returns target weights with the same shape (or a subset of columns).
WeightStrategy = "Callable[[pd.DataFrame], pd.DataFrame | pd.Series]"


@dataclass
class PortfolioOutput:
    returns: pd.Series          # per-bar net portfolio return
    equity: pd.Series           # equity curve
    weights: pd.DataFrame       # realized (post-shift) weights per asset
    gross_exposure: pd.Series   # sum(|w|) per bar
    net_exposure: pd.Series     # sum(w) per bar
    trades: pd.DataFrame        # round-trip trades (long & short), with return_pct
    initial_capital: float


def run_portfolio_backtest(
    prices: pd.DataFrame,
    strategy,
    *,
    volumes: pd.DataFrame | None = None,
    initial_capital: float = 100_000.0,
    cost_model: CostModel | None = None,
    borrow_bps_annual: float = 0.0,     # short-borrow financing (annualized)
    max_gross_leverage: float | None = None,
) -> PortfolioOutput:
    if prices.empty:
        raise ValueError("no price data to backtest")
    prices = prices.sort_index().astype(float)
    model = cost_model or CostModel()

    # Normalize the strategy output to a weights frame aligned to prices.
    raw = strategy(prices)
    target = _as_weight_frame(raw, prices)

    # Optional gross-leverage cap: scale DOWN only (never lever up a strategy).
    if max_gross_leverage is not None:
        gross = target.abs().sum(axis=1).replace(0.0, np.nan)
        factor = (max_gross_leverage / gross).clip(upper=1.0).fillna(1.0)
        target = target.mul(factor, axis=0)

    # NO-LOOKAHEAD: hold the prior bar's target this bar.
    position = target.shift(1).fillna(0.0)

    asset_returns = prices.pct_change().fillna(0.0)
    gross_ret = (position * asset_returns).sum(axis=1)

    # Per-asset turnover costs (commission + spread + sqrt impact), summed.
    cost = pd.Series(0.0, index=prices.index)
    for col in position.columns:
        vol = (volumes[col].astype(float) if volumes is not None and col in volumes
               else pd.Series(0.0, index=prices.index))
        cost = cost.add(model.per_bar_cost(position[col], prices[col], vol, initial_capital),
                        fill_value=0.0)

    # Short-borrow financing on the short notional, per bar.
    borrow = pd.Series(0.0, index=prices.index)
    if borrow_bps_annual > 0:
        ppy = infer_periods_per_year(prices.index)
        per_bar = (borrow_bps_annual / 10_000.0) / ppy
        short_notional = position.clip(upper=0.0).abs().sum(axis=1)
        borrow = short_notional * per_bar

    net = gross_ret - cost - borrow
    equity = (1.0 + net).cumprod() * initial_capital

    return PortfolioOutput(
        returns=net,
        equity=equity,
        weights=position,
        gross_exposure=position.abs().sum(axis=1),
        net_exposure=position.sum(axis=1),
        trades=_extract_trades_signed(position, prices),
        initial_capital=initial_capital,
    )


def _as_weight_frame(raw, prices: pd.DataFrame) -> pd.DataFrame:
    """Accept a Series (single-asset) or DataFrame (multi-asset) and return a
    weights frame aligned to the price panel's index & columns."""
    if isinstance(raw, pd.Series):
        # Single-asset: attach to the sole price column.
        if prices.shape[1] != 1:
            raise ValueError("Series signal requires a single-asset price frame")
        raw = raw.to_frame(prices.columns[0])
    return raw.reindex(index=prices.index, columns=prices.columns).astype(float).fillna(0.0)


def _extract_trades_signed(position: pd.DataFrame, prices: pd.DataFrame) -> pd.DataFrame:
    """Round-trip trades per asset, correct for BOTH long and short legs:
    a short's return is +ve when the price falls. A flip (long->short) closes
    the old leg and opens a new one."""
    records = []
    for col in position.columns:
        pos = position[col]
        px = prices[col]
        entry_px = None
        entry_sign = 0
        prev = 0.0
        for ts, w in pos.items():
            sign = 0 if w == 0 else (1 if w > 0 else -1)
            if sign != np.sign(prev):
                # close an open leg
                if entry_sign != 0 and entry_px is not None:
                    ret = entry_sign * (px.loc[ts] / entry_px - 1.0) * 100.0
                    records.append({"asset": col, "exit_time": ts,
                                    "direction": "long" if entry_sign > 0 else "short",
                                    "return_pct": ret})
                    entry_sign = 0
                    entry_px = None
                # open a new leg if not flat
                if sign != 0:
                    entry_px = px.loc[ts]
                    entry_sign = sign
            prev = w
    return pd.DataFrame(records, columns=["asset", "exit_time", "direction", "return_pct"])
