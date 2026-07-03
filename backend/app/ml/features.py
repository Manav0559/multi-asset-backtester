"""Feature engineering for ML strategies.

Every feature here is STRICTLY backward-looking: the value at bar t is a
function of prices/volumes at times <= t only. There is no `.shift(-k)`
anywhere — that would leak the future into the features and is the single
most common way student quant projects inflate their backtests. The
label module handles the (forward-looking) target separately; keeping the
two apart is what makes leakage auditable.

`build_features` returns a DataFrame aligned to the input index with the
warmup rows (that lack enough history) dropped.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

FEATURE_COLUMNS = [
    "ret_1", "ret_5", "ret_10",
    "mom_10", "mom_20",
    "vol_10", "vol_20",
    "rsi_14",
    "macd_hist",
    "sma_ratio_20", "sma_ratio_50",
    "vol_z_20",
    "hl_range",
]


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return (100.0 - 100.0 / (1.0 + rs)).fillna(50.0)


def _ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    close = df["close"].astype(float)
    volume = df["volume"].astype(float) if "volume" in df else pd.Series(0.0, index=close.index)
    high = df["high"].astype(float) if "high" in df else close
    low = df["low"].astype(float) if "low" in df else close

    ret = close.pct_change()
    macd_line = _ema(close, 12) - _ema(close, 26)
    macd_signal = _ema(macd_line, 9)

    feats = pd.DataFrame(index=close.index)
    feats["ret_1"] = ret
    feats["ret_5"] = close.pct_change(5)
    feats["ret_10"] = close.pct_change(10)
    feats["mom_10"] = close / close.shift(10) - 1.0
    feats["mom_20"] = close / close.shift(20) - 1.0
    feats["vol_10"] = ret.rolling(10).std()
    feats["vol_20"] = ret.rolling(20).std()
    feats["rsi_14"] = _rsi(close, 14)
    feats["macd_hist"] = macd_line - macd_signal
    feats["sma_ratio_20"] = close / close.rolling(20).mean() - 1.0
    feats["sma_ratio_50"] = close / close.rolling(50).mean() - 1.0
    vol_mean = volume.rolling(20).mean()
    vol_std = volume.rolling(20).std().replace(0.0, np.nan)
    feats["vol_z_20"] = ((volume - vol_mean) / vol_std).fillna(0.0)
    feats["hl_range"] = (high - low) / close

    return feats[FEATURE_COLUMNS].replace([np.inf, -np.inf], np.nan).dropna()
