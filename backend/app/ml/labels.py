"""Label construction for supervised strategies.

The label is the ONLY forward-looking quantity in the ML pipeline, and it
is isolated here on purpose. `forward_return_label` marks whether the
`horizon`-bar-ahead return exceeds a threshold — a binary up/not-up target.

Because the label at bar t peeks `horizon` bars into the future, the last
`horizon` rows have no valid label and are dropped. When these labels are
used with walk-forward CV, the embargo (>= horizon) prevents a training
label's lookahead window from overlapping the test set.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def forward_return(close: pd.Series, horizon: int = 1) -> pd.Series:
    return close.shift(-horizon) / close - 1.0


def forward_return_label(close: pd.Series, horizon: int = 1,
                         threshold: float = 0.0) -> pd.Series:
    """1 if the forward `horizon`-bar return > threshold, else 0.
    Trailing rows without a full forward window are dropped."""
    fwd = forward_return(close, horizon)
    label = (fwd > threshold).astype(int)
    label[fwd.isna()] = pd.NA
    return label.dropna().astype(int)


def triple_barrier_label(close: pd.Series, *, pt_mult: float = 2.0,
                         sl_mult: float = 1.0, max_horizon: int = 10,
                         vol_lookback: int = 20,
                         side: pd.Series | None = None) -> pd.DataFrame:
    """First-touch triple-barrier labels (López de Prado).

    For each bar t, three barriers on the forward path: profit-take at
    +pt_mult·σ_t, stop-loss at −sl_mult·σ_t (σ_t = EWMA vol of 1-bar returns,
    backward-only), and a vertical barrier max_horizon bars out.
    label = +1 (PT touched first), −1 (SL first), 0 (timeout).

    `side` (+1 long / −1 short) orients the path per bar — for a short, the
    profit barrier is DOWN. This is what meta-labeling trains on: "given the
    primary's side, did the trade pay before it stopped out?"

    Fixed-horizon labels ask "where is price in h bars"; barrier labels ask
    what a trader actually holds through — path-dependent and side-aware.
    A label at t peeks at most max_horizon bars ahead, so CV purge must be
    >= max_horizon. Returns (label, touch_ret, touch_bars) indexed like
    `close`, minus the σ warm-up and the tail without a full forward window.
    """
    px = close.astype(float)
    sigma = px.pct_change().ewm(span=vol_lookback, min_periods=vol_lookback).std()
    n = len(px)
    values = px.to_numpy()
    sig = sigma.to_numpy()
    sides = (side.reindex(px.index).fillna(0.0).to_numpy()
             if side is not None else np.ones(n))

    labels = np.full(n, np.nan)
    touch_ret = np.full(n, np.nan)
    touch_bars = np.full(n, np.nan)
    for t in range(n - max_horizon):  # tail rows lack a full forward window
        s = sig[t]
        if not np.isfinite(s) or s <= 0 or sides[t] == 0:
            continue
        path = (values[t + 1: t + 1 + max_horizon] / values[t] - 1.0) * sides[t]
        pt, sl = pt_mult * s, -sl_mult * s
        hit_pt = int(np.argmax(path >= pt)) if (path >= pt).any() else max_horizon
        hit_sl = int(np.argmax(path <= sl)) if (path <= sl).any() else max_horizon
        if hit_pt < hit_sl:
            labels[t], touch_ret[t], touch_bars[t] = 1.0, path[hit_pt], hit_pt + 1
        elif hit_sl < hit_pt:
            labels[t], touch_ret[t], touch_bars[t] = -1.0, path[hit_sl], hit_sl + 1
        else:  # neither barrier inside the window: vertical timeout
            labels[t], touch_ret[t], touch_bars[t] = 0.0, path[-1], max_horizon

    out = pd.DataFrame({"label": labels, "touch_ret": touch_ret,
                        "touch_bars": touch_bars}, index=px.index)
    return out.dropna(subset=["label"]).astype({"label": int, "touch_bars": int})
