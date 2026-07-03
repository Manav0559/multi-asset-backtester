"""Classic quant algorithms, implemented on the BaseStrategy contract.

These join the pre-existing function-style strategies (sma_crossover,
cross_sectional_momentum, ...) in the StrategyRegistry; class-based is the
canonical shape going forward because it carries its own metadata and params.
Indicator math comes from IndicatorService (pandas-ta) so a chart overlay and
a backtest of the same indicator can never disagree.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.backtest.base import BaseStrategy
from app.indicators import IndicatorService, IndicatorSpec


class MomentumBreakout(BaseStrategy):
    """Donchian channel breakout: long when close breaks above the prior
    `entry`-bar high, exit when it breaks below the prior `exit_`-bar low.
    The turtle-trader classic."""

    key = "momentum_breakout"
    description = "Donchian breakout — long above N-bar high, exit below M-bar low"
    category = "trend"

    def __init__(self, entry: int = 55, exit_: int = 20):
        if entry <= 1 or exit_ <= 1:
            raise ValueError("entry and exit_ must be > 1")
        super().__init__(entry=entry, exit_=exit_)

    def generate(self, data: pd.DataFrame) -> pd.Series:
        close = data["close"].astype(float)
        # shift(1): today's breakout is judged against the channel of PRIOR bars
        upper = close.rolling(self.params["entry"]).max().shift(1)
        lower = close.rolling(self.params["exit_"]).min().shift(1)
        signal = pd.Series(np.nan, index=close.index)
        signal[close > upper] = 1.0
        signal[close < lower] = 0.0
        return signal.ffill().fillna(0.0)


class BollingerReversion(BaseStrategy):
    """Mean reversion: long when close pierces the lower Bollinger band,
    exit when it reverts to the middle band (SMA)."""

    key = "bollinger_reversion"
    description = "Long below the lower Bollinger band, exit at the mid band"
    category = "mean_reversion"

    def __init__(self, length: int = 20, std: float = 2.0):
        super().__init__(length=length, std=std)

    def generate(self, data: pd.DataFrame) -> pd.Series:
        bands = IndicatorService.compute(
            data, [IndicatorSpec("bbands", {"length": self.params["length"],
                                            "lower_std": self.params["std"],
                                            "upper_std": self.params["std"]})])
        lower = bands.filter(like="BBL").iloc[:, 0]
        mid = bands.filter(like="BBM").iloc[:, 0]
        close = data["close"].astype(float)
        signal = pd.Series(np.nan, index=close.index)
        signal[close < lower] = 1.0
        signal[close > mid] = 0.0
        return signal.ffill().fillna(0.0)


class MacdTrend(BaseStrategy):
    """Trend following: long while the MACD line is above its signal line."""

    key = "macd_trend"
    description = "Long while MACD line > signal line"
    category = "trend"

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9):
        if fast >= slow:
            raise ValueError("fast must be < slow")
        super().__init__(fast=fast, slow=slow, signal=signal)

    def generate(self, data: pd.DataFrame) -> pd.Series:
        p = self.params
        macd = IndicatorService.compute(
            data, [IndicatorSpec("macd", {"fast": p["fast"], "slow": p["slow"],
                                          "signal": p["signal"]})])
        line = macd.iloc[:, 0]      # MACD_f_s_sig
        sig_line = macd.iloc[:, 2]  # MACDs_f_s_sig
        out = (line > sig_line).astype(float)
        out[line.isna() | sig_line.isna()] = 0.0
        return out


class PairsTrading(BaseStrategy):
    """Statistical arbitrage on a pair: z-score of the log-price spread,
    dollar-neutral. Short the spread (short A, long B) when z > entry_z,
    long the spread when z < -entry_z, flat inside exit_z. Requires exactly
    two assets in the basket."""

    key = "pairs_trading"
    description = "Stat-arb pair: trade the z-scored log-price spread, dollar-neutral"
    category = "arbitrage"
    kind = "portfolio"

    def __init__(self, lookback: int = 60, entry_z: float = 2.0, exit_z: float = 0.5):
        if exit_z >= entry_z:
            raise ValueError("exit_z must be < entry_z")
        super().__init__(lookback=lookback, entry_z=entry_z, exit_z=exit_z)

    def generate(self, prices: pd.DataFrame) -> pd.DataFrame:
        if prices.shape[1] != 2:
            raise ValueError("pairs_trading requires exactly 2 assets")
        p = self.params
        a, b = prices.columns[0], prices.columns[1]
        spread = np.log(prices[a].astype(float)) - np.log(prices[b].astype(float))
        mean = spread.rolling(p["lookback"]).mean()
        std = spread.rolling(p["lookback"]).std()
        z = (spread - mean) / std.replace(0.0, np.nan)

        # +1: long the spread (long A / short B); -1: the reverse; 0: flat.
        state = pd.Series(np.nan, index=prices.index)
        state[z < -p["entry_z"]] = 1.0
        state[z > p["entry_z"]] = -1.0
        state[z.abs() < p["exit_z"]] = 0.0
        state = state.ffill().fillna(0.0)

        weights = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
        weights[a] = 0.5 * state
        weights[b] = -0.5 * state
        return weights


CLASSIC_STRATEGIES = [MomentumBreakout, BollingerReversion, MacdTrend, PairsTrading]
