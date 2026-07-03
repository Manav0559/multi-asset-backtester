"""Long/short multi-asset engine tests.

These assert the properties that make the new signal contract trustworthy:
shorting P&L has the right SIGN, multi-asset returns aggregate correctly,
leverage is capped, borrow costs bite shorts, and no-lookahead still holds.
"""
import numpy as np
import pandas as pd
import pytest

from app.backtest.costs import CostModel
from app.backtest.portfolio_engine import run_portfolio_backtest
from app.backtest.portfolio_strategies import (
    cross_sectional_momentum,
    equal_weight_long,
    long_short_sma,
)


def _panel(data: dict, start="2020-01-01"):
    n = len(next(iter(data.values())))
    idx = pd.date_range(start, periods=n, freq="D", tz="UTC")
    return pd.DataFrame(data, index=idx)


# ------------------------------------------------------------- shorting --
def test_short_profits_when_price_falls():
    """A constant -1 (short) weight on a falling asset must GROW equity."""
    prices = _panel({"A": [100, 95, 90, 85, 80]})
    strat = lambda p: pd.Series(-1.0, index=p.index)  # always short
    out = run_portfolio_backtest(prices, strat)
    assert out.equity.iloc[-1] > out.initial_capital
    # net exposure is negative (we're short)
    assert (out.net_exposure <= 0).all()


def test_short_loses_when_price_rises():
    prices = _panel({"A": [100, 105, 110, 116]})
    out = run_portfolio_backtest(prices, lambda p: pd.Series(-1.0, index=p.index))
    assert out.equity.iloc[-1] < out.initial_capital


def test_long_and_short_are_mirror_images():
    prices = _panel({"A": [100, 110, 121]})
    up = run_portfolio_backtest(prices, lambda p: pd.Series(1.0, index=p.index))
    dn = run_portfolio_backtest(prices, lambda p: pd.Series(-1.0, index=p.index))
    # long return ~ +21%, short ~ -21% (before compounding asymmetry)
    assert up.returns.sum() == pytest.approx(-dn.returns.sum(), rel=1e-9)


# ------------------------------------------------------- multi-asset math --
def test_dollar_neutral_long_short_pair():
    """Long A (rises) + short B (falls), dollar-neutral. Both legs make money."""
    prices = _panel({"A": [100, 110, 121], "B": [100, 90, 81]})
    def strat(p):
        w = pd.DataFrame(0.0, index=p.index, columns=p.columns)
        w["A"] = 0.5
        w["B"] = -0.5
        return w
    out = run_portfolio_backtest(prices, strat)
    assert out.equity.iloc[-1] > out.initial_capital
    # dollar-neutral: gross ~1, net ~0
    assert out.gross_exposure.iloc[-1] == pytest.approx(1.0)
    assert out.net_exposure.iloc[-1] == pytest.approx(0.0)


def test_portfolio_return_equals_weighted_asset_returns():
    prices = _panel({"A": [100, 110], "B": [100, 105]})
    def strat(p):
        w = pd.DataFrame(0.0, index=p.index, columns=p.columns)
        w["A"] = 0.6
        w["B"] = 0.4
        return w
    out = run_portfolio_backtest(prices, strat)
    # realized on bar 1 (weights from bar 0): 0.6*10% + 0.4*5% = 8%
    assert out.returns.iloc[1] == pytest.approx(0.08, rel=1e-9)


# ----------------------------------------------------------- leverage cap --
def test_gross_leverage_is_capped():
    prices = _panel({"A": [100, 101, 102], "B": [100, 101, 102]})
    def strat(p):  # asks for gross 3.0 (1.5 + 1.5)
        w = pd.DataFrame({"A": 1.5, "B": 1.5}, index=p.index)
        return w
    out = run_portfolio_backtest(prices, strat, max_gross_leverage=1.0)
    assert out.gross_exposure.max() <= 1.0 + 1e-9


# ------------------------------------------------------------- borrow cost --
def test_borrow_cost_reduces_short_pnl():
    prices = _panel({"A": [100.0] * 260})  # flat price, so only costs matter
    strat = lambda p: pd.Series(-1.0, index=p.index)
    free = run_portfolio_backtest(prices, strat, borrow_bps_annual=0)
    charged = run_portfolio_backtest(prices, strat, borrow_bps_annual=200)
    assert charged.equity.iloc[-1] < free.equity.iloc[-1]


# ------------------------------------------------------------ no-lookahead --
def test_no_lookahead_multi_asset():
    prices = _panel({"A": [100, 200, 200], "B": [100, 50, 50]})
    def strat(p):
        return pd.DataFrame({"A": 1.0, "B": -1.0}, index=p.index)
    out = run_portfolio_backtest(prices, strat)
    assert out.returns.iloc[0] == 0.0  # first bar: no prior position


# -------------------------------------------------------- ref strategies --
def test_long_short_sma_takes_short_positions():
    # downtrend so the fast MA sits below slow -> short
    prices = _panel({"A": list(range(100, 40, -1))})
    out = run_portfolio_backtest(prices, long_short_sma(3, 8))
    assert (out.weights["A"] < 0).any()   # actually shorted


def test_cross_sectional_momentum_is_dollar_neutral():
    rng = np.random.RandomState(0)
    data = {c: list(100 + np.cumsum(rng.normal(0, 1, 200))) for c in ["A", "B", "C", "D"]}
    prices = _panel(data)
    out = run_portfolio_backtest(prices, cross_sectional_momentum(20, top_k=1, bottom_k=1))
    active = out.net_exposure[out.gross_exposure > 0]
    # every active bar is market-neutral within rounding
    assert np.allclose(active.values, 0.0, atol=1e-9)
