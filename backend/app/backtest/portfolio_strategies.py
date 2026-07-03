"""Reference weight-strategies for the long/short multi-asset engine.

Each returns TARGET WEIGHTS (see portfolio_engine): a Series for
single-asset, a DataFrame for multi-asset. Negative weights = short.
These are the tested exemplars of the general contract; user/ML strategies
plug in the same way.
"""
from __future__ import annotations

from typing import Callable

import numpy as np
import pandas as pd


def long_short_sma(fast: int = 20, slow: int = 50) -> Callable[[pd.DataFrame], pd.Series]:
    """Single asset: +1 (long) when fast SMA > slow SMA, -1 (short) otherwise.
    Unlike the long/flat version, this profits from downtrends too."""
    if fast >= slow:
        raise ValueError("fast must be < slow")

    def _strategy(prices: pd.DataFrame) -> pd.Series:
        close = prices.iloc[:, 0].astype(float)
        fast_ma = close.rolling(fast).mean()
        slow_ma = close.rolling(slow).mean()
        w = pd.Series(0.0, index=close.index)
        w[fast_ma > slow_ma] = 1.0
        w[fast_ma < slow_ma] = -1.0
        w[slow_ma.isna()] = 0.0
        return w

    return _strategy


def cross_sectional_momentum(lookback: int = 60, top_k: int = 1, bottom_k: int = 1,
                             gross: float = 1.0) -> Callable[[pd.DataFrame], pd.DataFrame]:
    """Multi-asset, dollar-neutral: each bar, rank assets by trailing
    `lookback` return; long the top_k, short the bottom_k, equally weighted so
    the book is market-neutral (sum of weights == 0). Classic long/short
    equity factor."""

    def _strategy(prices: pd.DataFrame) -> pd.DataFrame:
        mom = prices.pct_change(lookback)
        weights = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
        long_w = (gross / 2.0) / top_k
        short_w = (gross / 2.0) / bottom_k
        # Only rank rows where enough assets have a defined momentum.
        for ts, row in mom.iterrows():
            valid = row.dropna()
            if len(valid) < top_k + bottom_k:
                continue
            ranked = valid.sort_values(ascending=False)
            for a in ranked.index[:top_k]:
                weights.at[ts, a] = long_w
            for a in ranked.index[-bottom_k:]:
                weights.at[ts, a] = -short_w
        return weights

    return _strategy


def equal_weight_long(assets: list[str] | None = None) -> Callable[[pd.DataFrame], pd.DataFrame]:
    """Baseline: equal-weight long all assets (a naive index)."""
    def _strategy(prices: pd.DataFrame) -> pd.DataFrame:
        n = prices.shape[1]
        return pd.DataFrame(1.0 / n, index=prices.index, columns=prices.columns)
    return _strategy


PORTFOLIO_STRATEGIES: dict[str, Callable[..., object]] = {
    "long_short_sma": long_short_sma,
    "cross_sectional_momentum": cross_sectional_momentum,
    "equal_weight_long": equal_weight_long,
}
