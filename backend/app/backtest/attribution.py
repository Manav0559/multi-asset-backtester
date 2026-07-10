"""Factor attribution — where did the returns actually come from?

Regresses a backtest's per-bar returns on three factors built from OUR OWN
stored universe (same asset class as the backtest):

    MKT  equal-weight universe return (the market you actually trade in)
    MOM  winners-minus-losers: top tercile by trailing (126-5)-bar return
         minus bottom tercile, equal-weight, rebalanced daily
    LIQ  low-minus-high trailing 63-bar average dollar volume — a LIQUIDITY
         (size-proxy) factor, honestly labeled: we store no market caps, so
         this is ADV-based SMB, not true SMB

    r_p,t = alpha + b_mkt·MKT_t + b_mom·MOM_t + b_liq·LIQ_t + eps_t   (OLS)

A "momentum strategy" with beta_mom ≈ 1 and alpha ≈ 0 is factor exposure you
could buy for 20bp, not alpha — that's the question this card answers.
Plain OLS with an intercept; no HAC correction (noted in the payload) — a
resume-honest v1, not a risk system.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.backtest.metrics import infer_periods_per_year

MIN_UNIVERSE = 10   # fewer names make tercile portfolios degenerate
MIN_OBS = 60        # fewer aligned bars make the regression noise
_MOM_LOOKBACK, _MOM_SKIP = 126, 5
_LIQ_LOOKBACK = 63


def build_factors(closes: pd.DataFrame, volumes: pd.DataFrame) -> pd.DataFrame:
    """Factor return series from aligned close/volume panels (bars x assets).
    Terciles recomputed each bar on trailing data only — no lookahead: the
    ranking at t uses returns/volumes up to t, applied to the t -> t+1 return
    via the same shift(1) discipline as the engines."""
    rets = closes.pct_change()

    # MKT: equal-weight mean across whatever is alive that bar.
    mkt = rets.mean(axis=1)

    # MOM: trailing (lookback - skip) return, skipping the most recent bars
    # (short-term reversal contaminates plain momentum).
    mom_score = closes.shift(_MOM_SKIP) / closes.shift(_MOM_LOOKBACK) - 1.0
    mom = _tercile_spread(rets, mom_score)

    # LIQ: LOW minus HIGH trailing average dollar volume (illiquid-minus-
    # liquid — the size-proxy direction: small names are the illiquid ones).
    adv = (closes * volumes).rolling(_LIQ_LOOKBACK).mean()
    liq = -_tercile_spread(rets, adv)

    return pd.DataFrame({"MKT": mkt, "MOM": mom, "LIQ": liq}).dropna()


def _tercile_spread(rets: pd.DataFrame, score: pd.DataFrame) -> pd.Series:
    """Equal-weight top-tercile minus bottom-tercile return, scores lagged one
    bar so the portfolio formed at t-1 earns the t return."""
    lagged = score.shift(1)
    ranks = lagged.rank(axis=1, pct=True)
    top = rets.where(ranks >= 2 / 3)
    bot = rets.where(ranks <= 1 / 3)
    return top.mean(axis=1) - bot.mean(axis=1)


def attribute(returns: pd.Series, factors: pd.DataFrame) -> dict | None:
    """OLS of strategy returns on the factor panel. Returns the diagnostics
    payload, or None when there's not enough aligned data to be honest."""
    df = pd.concat([returns.rename("r"), factors], axis=1, join="inner").dropna()
    if len(df) < MIN_OBS:
        return None
    y = df["r"].to_numpy()
    names = list(factors.columns)
    X = np.column_stack([np.ones(len(df))] + [df[c].to_numpy() for c in names])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ coef
    ss_res = float(resid @ resid)
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    ppy = infer_periods_per_year(df.index)
    return {
        "alpha_annual_pct": round(float(coef[0]) * ppy * 100.0, 4),
        "betas": {n: round(float(b), 4) for n, b in zip(names, coef[1:])},
        "r_squared": round(r2, 4),
        "n_obs": int(len(df)),
        "factors_note": ("factors built from the stored universe; LIQ is an "
                         "ADV-based size proxy (no market-cap data); plain OLS, "
                         "no HAC correction"),
    }
