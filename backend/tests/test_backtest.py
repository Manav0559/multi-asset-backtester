"""Backtest engine + metrics tests.

Uses deterministic synthetic price series with KNOWN outcomes so every
assertion has a ground truth — including the no-lookahead guarantee, which
is the single most important quant-correctness property.
"""
import math

import numpy as np
import pandas as pd
import pytest

from app.backtest.engine import run_backtest
from app.backtest.metrics import (
    compute_metrics,
    deflated_sharpe_ratio,
    infer_periods_per_year,
    max_drawdown_pct,
    yearly_breakdown,
)
from app.backtest.strategies import buy_and_hold, sma_crossover


def _series(prices, start="2020-01-01", freq="D"):
    idx = pd.date_range(start, periods=len(prices), freq=freq, tz="UTC")
    return pd.DataFrame({"open": prices, "high": prices, "low": prices,
                         "close": prices, "volume": [1] * len(prices)}, index=idx)


# ------------------------------------------------------------- engine core --
def test_buy_and_hold_matches_price_return():
    df = _series([100, 110, 121])  # +10% then +10%
    out = run_backtest(df, buy_and_hold(), initial_capital=1000)
    # first bar has no prior position (shift) -> return 0; then compounding
    assert out.equity.iloc[-1] == pytest.approx(1000 * 1.10 * 1.10, rel=1e-9)


def test_no_lookahead_first_signal_has_no_effect():
    """A strategy that is long from bar 0 must NOT capture bar 0->1 return,
    because position_t = signal_{t-1}. This is the leakage guard."""
    df = _series([100, 200, 200])  # 100% jump on the first step
    out = run_backtest(df, buy_and_hold(), initial_capital=1000)
    # If lookahead leaked, equity would double on the first step. It must not.
    assert out.returns.iloc[0] == 0.0
    assert out.returns.iloc[1] == pytest.approx(1.0)  # captured on the SECOND step


def test_sma_crossover_generates_expected_position():
    # ramp up then down so a fast/slow crossover definitely flips
    prices = list(range(1, 30)) + list(range(29, 0, -1))
    df = _series(prices)
    out = run_backtest(df, sma_crossover(fast=3, slow=8), initial_capital=1000)
    assert set(out.position.unique()) <= {0.0, 1.0}
    assert out.trades.shape[0] >= 1  # at least one round trip


def test_commission_reduces_return():
    df = _series([100, 110, 100, 110])
    free = run_backtest(df, sma_crossover(fast=1, slow=2), commission_bps=0)
    costly = run_backtest(df, sma_crossover(fast=1, slow=2), commission_bps=50)
    assert costly.equity.iloc[-1] <= free.equity.iloc[-1]


# ----------------------------------------------------------------- metrics --
def test_max_drawdown_known_value():
    equity = pd.Series([100, 120, 60, 90],
                       index=pd.date_range("2020-01-01", periods=4, tz="UTC"))
    # peak 120 -> trough 60 = -50%
    assert max_drawdown_pct(equity) == pytest.approx(-50.0)


def test_sharpe_zero_for_flat_returns():
    df = _series([100] * 300)
    out = run_backtest(df, buy_and_hold())
    m = compute_metrics(out.returns, out.equity, out.trades)
    assert m.sharpe == 0.0
    assert m.max_drawdown_pct == 0.0


def test_periods_per_year_daily_is_252():
    idx = pd.date_range("2020-01-01", periods=100, freq="D", tz="UTC")
    assert infer_periods_per_year(idx) == pytest.approx(252.0)


def test_yearly_breakdown_splits_by_calendar_year():
    # 2 years of daily data
    idx = pd.date_range("2020-01-01", periods=500, freq="D", tz="UTC")
    prices = 100 * (1.0005 ** np.arange(500))  # steady uptrend
    df = pd.DataFrame({"open": prices, "high": prices, "low": prices,
                       "close": prices, "volume": 1}, index=idx)
    out = run_backtest(df, buy_and_hold())
    yb = yearly_breakdown(out.returns, out.equity, out.trades)
    years = {y.year for y in yb}
    assert 2020 in years and 2021 in years
    for y in yb:
        assert y.return_pct > 0  # uptrend every year


# ------------------------------------------------- deflated sharpe (guard) --
def test_deflated_sharpe_decreases_with_more_trials():
    np.random.seed(0)
    idx = pd.date_range("2018-01-01", periods=1000, freq="D", tz="UTC")
    returns = pd.Series(np.random.normal(0.0005, 0.01, 1000), index=idx)
    dsr_1 = deflated_sharpe_ratio(returns, n_trials=1, sr_variance=0.001)
    dsr_100 = deflated_sharpe_ratio(returns, n_trials=100, sr_variance=0.001)
    # Trying more strategies raises the bar -> the SAME track record is less
    # convincing -> lower DSR probability.
    assert dsr_100 < dsr_1
    assert 0.0 <= dsr_100 <= 1.0 and 0.0 <= dsr_1 <= 1.0


def test_deflated_sharpe_high_for_strong_consistent_returns():
    idx = pd.date_range("2015-01-01", periods=1500, freq="D", tz="UTC")
    returns = pd.Series(np.full(1500, 0.001), index=idx)  # tiny but perfectly steady
    returns += np.random.RandomState(1).normal(0, 0.0001, 1500)
    dsr = deflated_sharpe_ratio(returns, n_trials=1, sr_variance=0.0)
    assert dsr > 0.9  # near-certain the true Sharpe is positive
