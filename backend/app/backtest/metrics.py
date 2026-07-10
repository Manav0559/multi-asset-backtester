"""Performance metrics + the Performance-Slicing (YoY) breakdown +
the Deflated Sharpe Ratio overfitting guard.

Annualization uses an inferred periods-per-year from the bar spacing
(≈252 for daily, 252*375 for 1-minute NSE, etc.), so the same code gives
correct annualized Sharpe/Sortino at any timeframe.

Deflated Sharpe Ratio (Bailey & López de Prado, 2014): when you try N
strategy variants and report the best, its Sharpe is upward-biased by
selection. DSR is the probability the *true* Sharpe is > 0 after correcting
for (a) the number of trials N, (b) the track-record length, and (c) the
return distribution's skew and kurtosis. Reporting DSR alongside the raw
Sharpe is the headline overfitting-honesty signal.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd
from scipy.stats import norm

_EULER_MASCHERONI = 0.5772156649015329


def infer_periods_per_year(index: pd.DatetimeIndex) -> float:
    if len(index) < 3:
        return 252.0
    median_dt = np.median(np.diff(index.values).astype("timedelta64[s]").astype(float))
    if median_dt <= 0:
        return 252.0
    seconds_per_year = 365.25 * 24 * 3600
    # Cap at 252 trading days for daily+; scale up for intraday.
    if median_dt >= 23 * 3600:      # ~daily or coarser
        return 252.0
    bars_per_day = (6.5 * 3600) / median_dt   # ~6.5h trading session
    return 252.0 * max(bars_per_day, 1.0)


@dataclass
class Metrics:
    total_return_pct: float
    cagr_pct: float
    sharpe: float
    sortino: float
    max_drawdown_pct: float
    volatility_pct: float
    trade_count: int
    win_rate_pct: float


def _sharpe(returns: pd.Series, ppy: float, rf: float = 0.0) -> float:
    excess = returns - rf / ppy
    sd = excess.std(ddof=1)
    if sd == 0 or math.isnan(sd):
        return 0.0
    return float(excess.mean() / sd * math.sqrt(ppy))


def _sortino(returns: pd.Series, ppy: float, rf: float = 0.0) -> float:
    excess = returns - rf / ppy
    downside = excess[excess < 0]
    dd = math.sqrt((downside.pow(2).sum()) / len(excess)) if len(excess) else 0.0
    if dd == 0 or math.isnan(dd):
        return 0.0
    return float(excess.mean() / dd * math.sqrt(ppy))


def max_drawdown_pct(equity: pd.Series) -> float:
    running_max = equity.cummax()
    drawdown = equity / running_max - 1.0
    return float(drawdown.min() * 100.0)


def tail_risk_metrics(returns: pd.Series) -> dict:
    """Historical VaR/ES + Cornish-Fisher modified VaR at 95%/99%.

    All values are POSITIVE loss fractions per period (the report layer
    formats as %); the period is whatever frequency the return series is —
    daily bars => daily VaR. Historical quantiles make no distributional
    assumption; Cornish-Fisher adjusts the normal quantile for the skew and
    excess kurtosis real strategy returns actually have:

        z_cf = z + (z²-1)·S/6 + (z³-3z)·K/24 - (2z³-5z)·S²/36

    Negative skew / fat tails push CF-VaR beyond normal VaR — which is the
    honest direction: the strategies that look smoothest carry the worst
    tails. ES (expected shortfall) answers "how bad is it WHEN it's bad" —
    the mean loss beyond VaR — and is the number an allocator actually asks.
    """
    r = returns.dropna()
    if len(r) < 20:  # too few observations for a meaningful tail estimate
        return {}
    mu, sd = float(r.mean()), float(r.std(ddof=1))
    skew, exkurt = float(r.skew()), float(r.kurt())  # pandas kurt() is EXCESS
    out: dict = {"skew": round(skew, 4), "excess_kurtosis": round(exkurt, 4)}
    for alpha in (0.95, 0.99):
        tag = str(int(alpha * 100))
        q = float(r.quantile(1.0 - alpha))
        tail = r[r <= q]
        out[f"var_{tag}"] = round(-q, 6)
        out[f"es_{tag}"] = round(float(-tail.mean()) if len(tail) else -q, 6)
        z = float(norm.ppf(1.0 - alpha))  # negative: left tail
        z_cf = (z + (z**2 - 1) * skew / 6
                + (z**3 - 3 * z) * exkurt / 24
                - (2 * z**3 - 5 * z) * skew**2 / 36)
        out[f"cf_var_{tag}"] = round(-(mu + z_cf * sd), 6)
    return out


def compute_metrics(returns: pd.Series, equity: pd.Series, trades: pd.DataFrame,
                    rf: float = 0.0) -> Metrics:
    ppy = infer_periods_per_year(equity.index)
    total_return = float(equity.iloc[-1] / equity.iloc[0] - 1.0)

    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1e-9)
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1.0 / years) - 1.0 if years > 0 else 0.0

    win_rate = (float((trades["return_pct"] > 0).mean()) * 100.0
                if not trades.empty else 0.0)

    return Metrics(
        total_return_pct=total_return * 100.0,
        cagr_pct=float(cagr) * 100.0,
        sharpe=_sharpe(returns, ppy, rf),
        sortino=_sortino(returns, ppy, rf),
        max_drawdown_pct=max_drawdown_pct(equity),
        volatility_pct=float(returns.std(ddof=1) * math.sqrt(ppy) * 100.0),
        trade_count=int(len(trades)),
        win_rate_pct=win_rate,
    )


@dataclass
class YearlyMetrics:
    year: int
    return_pct: float
    max_drawdown_pct: float
    sharpe: float
    sortino: float
    volatility_pct: float
    trade_count: int
    win_rate_pct: float


def yearly_breakdown(returns: pd.Series, equity: pd.Series, trades: pd.DataFrame,
                     rf: float = 0.0) -> list[YearlyMetrics]:
    """Performance Slicing: one YearlyMetrics per calendar year."""
    ppy = infer_periods_per_year(equity.index)
    out: list[YearlyMetrics] = []
    for year, yr_returns in returns.groupby(returns.index.year):
        yr_equity = (1.0 + yr_returns).cumprod()
        if not trades.empty:
            yr_trades = trades[pd.to_datetime(trades["exit_time"]).dt.year == year]
        else:
            yr_trades = trades
        out.append(YearlyMetrics(
            year=int(year),
            return_pct=float(yr_equity.iloc[-1] - 1.0) * 100.0,
            max_drawdown_pct=max_drawdown_pct(yr_equity),
            sharpe=_sharpe(yr_returns, ppy, rf),
            sortino=_sortino(yr_returns, ppy, rf),
            volatility_pct=float(yr_returns.std(ddof=1) * math.sqrt(ppy) * 100.0)
            if len(yr_returns) > 1 else 0.0,
            trade_count=int(len(yr_trades)),
            win_rate_pct=(float((yr_trades["return_pct"] > 0).mean()) * 100.0
                          if not yr_trades.empty else 0.0),
        ))
    return out


def probabilistic_sharpe_ratio(sr: float, n: int, skew: float, kurt: float,
                               sr_benchmark: float = 0.0) -> float:
    """PSR: P(true SR > sr_benchmark) given track-record length and the
    return distribution's higher moments. `sr` and `sr_benchmark` are
    per-period (non-annualized). `kurt` is the (non-excess) kurtosis."""
    if n < 2:
        return float("nan")
    denom = math.sqrt(max(1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr ** 2, 1e-12))
    z = (sr - sr_benchmark) * math.sqrt(n - 1) / denom
    return float(norm.cdf(z))


def _expected_max_sharpe(n_trials: int, sr_variance: float) -> float:
    """Expected maximum of N iid Sharpe estimates under the null (SR0).
    Uses the Gumbel/extreme-value approximation from López de Prado."""
    if n_trials <= 1 or sr_variance <= 0:
        return 0.0
    sigma = math.sqrt(sr_variance)
    inv1 = norm.ppf(1.0 - 1.0 / n_trials)
    inv2 = norm.ppf(1.0 - 1.0 / (n_trials * math.e))
    return sigma * ((1.0 - _EULER_MASCHERONI) * inv1 + _EULER_MASCHERONI * inv2)


def deflated_sharpe_ratio(returns: pd.Series, n_trials: int,
                          sr_variance: float | None = None) -> float:
    """DSR: PSR evaluated against the *expected maximum* Sharpe from N trials
    (SR0), instead of against zero. Higher N or higher trial-variance raises
    the bar, deflating an over-selected Sharpe toward 0.

    `sr_variance` is the variance of Sharpe estimates across the trials. When
    unknown (single run), we estimate it from the dispersion of yearly Sharpe
    sub-samples — a documented, self-contained proxy."""
    r = returns.dropna()
    n = len(r)
    if n < 3:
        return float("nan")
    sd = r.std(ddof=1)
    if sd == 0:
        return float("nan")
    sr = float(r.mean() / sd)                      # per-period Sharpe
    skew = float(r.skew())
    kurt = float(r.kurtosis() + 3.0)               # pandas gives excess kurtosis

    if sr_variance is None:
        yearly_sr = []
        for _, yr in r.groupby(r.index.year):
            if len(yr) > 2 and yr.std(ddof=1) > 0:
                yearly_sr.append(yr.mean() / yr.std(ddof=1))
        sr_variance = float(np.var(yearly_sr, ddof=1)) if len(yearly_sr) > 1 else 0.0

    sr0 = _expected_max_sharpe(n_trials, sr_variance)
    return probabilistic_sharpe_ratio(sr, n, skew, kurt, sr_benchmark=sr0)


def metrics_to_dict(m: Metrics) -> dict:
    return asdict(m)
