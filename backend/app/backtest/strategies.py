"""Strategy interface + built-in reference strategies.

A Strategy is a callable: (df) -> target-position Series in {0, 1} (long/flat
for v1; shorting is a later extension). It sees ONLY the OHLCV frame and must
be backward-looking — the engine enforces no-lookahead by shifting signals a
bar before they take effect, but strategies must not peek forward internally
(e.g. using .shift(-1)).

Built-ins are deterministic and used as the tested ground truth for the
engine. Arbitrary user-submitted strategies run through the same interface
later, inside the sandbox layer.
"""
from __future__ import annotations

from typing import Callable

import numpy as np
import pandas as pd

Strategy = Callable[[pd.DataFrame], pd.Series]


def sma_crossover(fast: int = 20, slow: int = 50) -> Strategy:
    """Long when fast SMA > slow SMA, else flat."""
    if fast >= slow:
        raise ValueError("fast window must be < slow window")

    def _strategy(df: pd.DataFrame) -> pd.Series:
        close = df["close"].astype(float)
        fast_ma = close.rolling(fast).mean()
        slow_ma = close.rolling(slow).mean()
        signal = (fast_ma > slow_ma).astype(float)
        signal[slow_ma.isna()] = 0.0  # no position until both MAs are defined
        return signal

    return _strategy


def rsi_reversion(period: int = 14, low: float = 30.0, high: float = 70.0) -> Strategy:
    """Mean-reversion: go long when RSI crosses below `low`, exit when it
    crosses above `high`. Holds between thresholds (stateful via ffill)."""

    def _strategy(df: pd.DataFrame) -> pd.Series:
        close = df["close"].astype(float)
        delta = close.diff()
        gain = delta.clip(lower=0.0)
        loss = -delta.clip(upper=0.0)
        avg_gain = gain.rolling(period).mean()
        avg_loss = loss.rolling(period).mean()
        rs = avg_gain / avg_loss.replace(0.0, np.nan)
        rsi = 100.0 - 100.0 / (1.0 + rs)

        signal = pd.Series(np.nan, index=close.index)
        signal[rsi < low] = 1.0
        signal[rsi > high] = 0.0
        return signal.ffill().fillna(0.0)

    return _strategy


def buy_and_hold() -> Strategy:
    """Baseline: always long. Useful as a benchmark row."""
    def _strategy(df: pd.DataFrame) -> pd.Series:
        return pd.Series(1.0, index=df.index)
    return _strategy


def _ml_direction(**params) -> Strategy:
    # Imported lazily so the base engine doesn't require xgboost/sklearn.
    from app.ml.model import ml_direction_strategy
    return ml_direction_strategy(**params)


BUILTIN_STRATEGIES: dict[str, Callable[..., Strategy]] = {
    "sma_crossover": sma_crossover,
    "rsi_reversion": rsi_reversion,
    "buy_and_hold": buy_and_hold,
    "ml_direction": _ml_direction,
}
