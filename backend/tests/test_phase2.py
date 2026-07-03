"""Phase 2 tests: cost model, feature leakage guard, walk-forward embargo,
and the XGBoost strategy's out-of-sample honesty.

The leakage/embargo tests are the ones that matter for a quant reviewer —
they assert the properties that make the ML results trustworthy.
"""
import numpy as np
import pandas as pd
import pytest

from app.backtest.costs import CostModel
from app.backtest.engine import run_backtest
from app.backtest.strategies import buy_and_hold
from app.ml.features import build_features
from app.ml.labels import forward_return_label
from app.ml.model import run_ml_direction
from app.ml.validation import walk_forward_splits


def _ohlcv(prices, vol=None):
    idx = pd.date_range("2015-01-01", periods=len(prices), freq="D", tz="UTC")
    v = vol if vol is not None else [1_000_000] * len(prices)
    return pd.DataFrame({"open": prices, "high": prices, "low": prices,
                         "close": prices, "volume": v}, index=idx)


# ------------------------------------------------------------- cost model --
def test_sqrt_impact_increases_with_participation():
    cm = CostModel(impact_coef=0.5)
    pos = pd.Series([0, 1, 1], dtype=float)
    close = pd.Series([100.0, 100.0, 100.0])
    liquid = cm.per_bar_cost(pos, close, pd.Series([1e9, 1e9, 1e9]), 1e6)
    illiquid = cm.per_bar_cost(pos, close, pd.Series([1e4, 1e4, 1e4]), 1e6)
    # Same trade into a thinner book costs strictly more (impact term).
    assert illiquid.iloc[1] > liquid.iloc[1] > 0


def test_impact_is_superlinear_sqrt():
    cm = CostModel(impact_coef=1.0)
    # participation quadruples -> sqrt-impact doubles
    c1 = cm.per_bar_cost(pd.Series([0.0, 1.0]), pd.Series([100.0, 100.0]),
                         pd.Series([1e6, 1e6]), 1e4)  # participation p
    c4 = cm.per_bar_cost(pd.Series([0.0, 1.0]), pd.Series([100.0, 100.0]),
                         pd.Series([1e6, 1e6]), 4e4)  # participation 4p
    assert c4.iloc[1] == pytest.approx(2.0 * c1.iloc[1], rel=1e-6)


def test_costs_reduce_equity():
    df = _ohlcv([100, 101, 100, 101, 100, 101] * 10)
    free = run_backtest(df, buy_and_hold(), cost_model=CostModel())
    costly = run_backtest(df, buy_and_hold(),
                          cost_model=CostModel(spread_bps=10, impact_coef=0.2))
    assert costly.equity.iloc[-1] <= free.equity.iloc[-1]


# ----------------------------------------------------- feature leakage guard --
def test_features_are_strictly_backward_looking():
    """Mutating a FUTURE price must not change any PAST feature row. This is
    the definitive no-lookahead test for the feature store."""
    prices = list(100 + np.cumsum(np.random.RandomState(0).normal(0, 1, 200)))
    base = build_features(_ohlcv(prices))

    tampered_prices = prices.copy()
    tampered_prices[-1] = tampered_prices[-1] * 2.0  # change only the last bar
    tampered = build_features(_ohlcv(tampered_prices))

    common = base.index.intersection(tampered.index)[:-1]  # exclude the tampered bar
    pd.testing.assert_frame_equal(base.loc[common], tampered.loc[common])


def test_forward_label_drops_tail_and_is_binary():
    close = pd.Series([100, 110, 105, 120, 118],
                      index=pd.date_range("2020-01-01", periods=5, tz="UTC"))
    y = forward_return_label(close, horizon=1)
    assert set(y.unique()) <= {0, 1}
    assert len(y) == 4  # last bar has no forward return


# ----------------------------------------------------- walk-forward + embargo --
def test_walk_forward_train_always_before_test():
    for train_idx, test_idx in walk_forward_splits(100, n_splits=5, embargo=0):
        assert train_idx.max() < test_idx.min()   # never train on the future


def test_embargo_purges_gap_between_train_and_test():
    embargo = 5
    for train_idx, test_idx in walk_forward_splits(100, n_splits=4, embargo=embargo):
        gap = test_idx.min() - train_idx.max() - 1
        assert gap >= embargo   # at least `embargo` bars purged


def test_folds_are_non_overlapping_and_forward():
    seen_test = []
    for _, test_idx in walk_forward_splits(120, n_splits=6, embargo=2):
        seen_test.append(test_idx)
    for a, b in zip(seen_test, seen_test[1:]):
        assert a.max() < b.min()  # test blocks march forward, no overlap


# ------------------------------------------------------- ML strategy honesty --
def test_ml_learns_a_real_momentum_pattern_out_of_sample():
    """Construct a series with genuine momentum autocorrelation. A correct
    OOS pipeline should beat a coin flip; a broken/leaky one would score ~1.0
    (too good) or ~0.5 by luck. We assert a sane, non-trivial OOS accuracy."""
    rng = np.random.RandomState(7)
    n = 800
    rets = np.zeros(n)
    for i in range(1, n):
        # momentum: today's drift leans on yesterday's return
        rets[i] = 0.5 * rets[i - 1] + rng.normal(0, 0.01)
    prices = 100 * np.exp(np.cumsum(rets))
    df = _ohlcv(prices)

    res = run_ml_direction(df, horizon=1, n_splits=5, embargo=1)
    assert 0.0 <= res.oos_accuracy <= 1.0
    assert res.oos_accuracy > 0.52          # learns the pattern, beats chance
    assert res.oos_accuracy < 0.95          # but NOT implausibly perfect (leak flag)
    assert set(np.unique(res.signal.values)) <= {0.0, 1.0}
    assert len(res.feature_importance) > 0


def test_ml_signal_is_oos_only_zero_before_first_test_fold():
    prices = 100 * np.exp(np.cumsum(np.random.RandomState(1).normal(0, 0.01, 600)))
    df = _ohlcv(prices)
    res = run_ml_direction(df, horizon=1, n_splits=5, embargo=1)
    # The initial training region must carry NO signal (no in-sample trading).
    first_signal_pos = np.argmax(res.signal.values != 0) if res.n_predictions else 0
    assert first_signal_pos > len(df) // 8
