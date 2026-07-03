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


class EmaCrossover(BaseStrategy):
    """Long while the fast EMA is above the slow EMA — the responsive cousin
    of the SMA cross (EMAs weight recent bars, so crossings come earlier)."""

    key = "ema_crossover"
    description = "Long while fast EMA > slow EMA"
    category = "trend"

    def __init__(self, fast: int = 12, slow: int = 26):
        if fast >= slow:
            raise ValueError("fast must be < slow")
        super().__init__(fast=fast, slow=slow)

    def generate(self, data: pd.DataFrame) -> pd.Series:
        close = data["close"].astype(float)
        fast = close.ewm(span=self.params["fast"], adjust=False).mean()
        slow = close.ewm(span=self.params["slow"], adjust=False).mean()
        out = (fast > slow).astype(float)
        out.iloc[: self.params["slow"]] = 0.0  # warm-up: EWMs not yet meaningful
        return out


class SupertrendTrend(BaseStrategy):
    """Long while Supertrend flips bullish (direction column > 0). ATR-banded
    trend following — stays in through noise, exits on regime flip."""

    key = "supertrend_trend"
    description = "Long while Supertrend direction is bullish"
    category = "trend"

    def __init__(self, length: int = 10, multiplier: float = 3.0):
        super().__init__(length=length, multiplier=multiplier)

    def generate(self, data: pd.DataFrame) -> pd.Series:
        st = IndicatorService.compute(
            data, [IndicatorSpec("supertrend", {"length": self.params["length"],
                                                "multiplier": self.params["multiplier"]})])
        direction = st.filter(like="SUPERTd").iloc[:, 0]
        out = (direction > 0).astype(float)
        out[direction.isna()] = 0.0
        return out


class StochasticReversion(BaseStrategy):
    """Mean reversion on the stochastic oscillator: long when %K dips below
    `low`, exit when it recovers above `high`."""

    key = "stochastic_reversion"
    description = "Long on stochastic %K oversold, exit on overbought"
    category = "mean_reversion"

    def __init__(self, k: int = 14, low: float = 20.0, high: float = 80.0):
        if low >= high:
            raise ValueError("low must be < high")
        super().__init__(k=k, low=low, high=high)

    def generate(self, data: pd.DataFrame) -> pd.Series:
        stoch = IndicatorService.compute(
            data, [IndicatorSpec("stoch", {"k": self.params["k"]})])
        k_line = stoch.filter(like="STOCHk").iloc[:, 0]
        signal = pd.Series(np.nan, index=data.index)
        signal[k_line < self.params["low"]] = 1.0
        signal[k_line > self.params["high"]] = 0.0
        return signal.ffill().fillna(0.0)


class AdxTrendFilter(BaseStrategy):
    """Directional trend filter: long only when the trend is STRONG
    (ADX > threshold) and bullish (+DI > -DI). Chop stays flat by design."""

    key = "adx_trend_filter"
    description = "Long when ADX confirms a strong bullish trend (+DI > -DI)"
    category = "trend"

    def __init__(self, length: int = 14, threshold: float = 25.0):
        super().__init__(length=length, threshold=threshold)

    def generate(self, data: pd.DataFrame) -> pd.Series:
        adx = IndicatorService.compute(
            data, [IndicatorSpec("adx", {"length": self.params["length"]})])
        strength = adx.filter(like="ADX").iloc[:, 0]
        plus = adx.filter(like="DMP").iloc[:, 0]
        minus = adx.filter(like="DMN").iloc[:, 0]
        out = ((strength > self.params["threshold"]) & (plus > minus)).astype(float)
        out[strength.isna()] = 0.0
        return out


class ZScoreReversion(BaseStrategy):
    """Textbook mean reversion: long when close sits `entry_z` std-devs below
    its rolling mean, exit when it reverts to the mean."""

    key = "zscore_reversion"
    description = "Long when price is entry_z σ below its rolling mean, exit at the mean"
    category = "mean_reversion"

    def __init__(self, lookback: int = 20, entry_z: float = 2.0):
        super().__init__(lookback=lookback, entry_z=entry_z)

    def generate(self, data: pd.DataFrame) -> pd.Series:
        close = data["close"].astype(float)
        mean = close.rolling(self.params["lookback"]).mean()
        std = close.rolling(self.params["lookback"]).std()
        z = (close - mean) / std.replace(0.0, np.nan)
        signal = pd.Series(np.nan, index=data.index)
        signal[z < -self.params["entry_z"]] = 1.0
        signal[z > 0] = 0.0
        return signal.ffill().fillna(0.0)


class AtrChannelBreakout(BaseStrategy):
    """Volatility-adjusted breakout (Keltner-style): long when close breaks
    above EMA + mult·ATR, exit when it falls back below the EMA."""

    key = "atr_channel_breakout"
    description = "Long above EMA + mult·ATR, exit back below the EMA"
    category = "trend"

    def __init__(self, length: int = 20, mult: float = 2.0):
        super().__init__(length=length, mult=mult)

    def generate(self, data: pd.DataFrame) -> pd.Series:
        close = data["close"].astype(float)
        ema = close.ewm(span=self.params["length"], adjust=False).mean()
        atr = IndicatorService.compute(
            data, [IndicatorSpec("atr", {"length": self.params["length"]})]).iloc[:, 0]
        signal = pd.Series(np.nan, index=data.index)
        signal[close > ema + self.params["mult"] * atr] = 1.0
        signal[close < ema] = 0.0
        signal[atr.isna()] = 0.0
        return signal.ffill().fillna(0.0)


CLASSIC_STRATEGIES = [
    MomentumBreakout, BollingerReversion, MacdTrend, PairsTrading,
    EmaCrossover, SupertrendTrend, StochasticReversion, AdxTrendFilter,
    ZScoreReversion, AtrChannelBreakout,
]
