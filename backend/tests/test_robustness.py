"""Robustness Certificates: block-bootstrap + jump-overlay stress search.

Deterministic-edge strategies (always-long / always-short on data with a
known drift sign) are used for the pass/fail assertions, deliberately
avoiding a real crossover strategy's inherent sequencing-sensitivity (see
robustness.py's module docstring) as a source of test flakiness -- that
variance is real and worth exercising once explicitly (see
test_sequencing_sensitive_strategy_has_wide_dsr_spread), just not as the
basis for a pass/fail assertion.
"""
import numpy as np
import pandas as pd
import pytest

from app.backtest.robustness import certify_robustness
from app.backtest.strategies import sma_crossover


def _trending_ohlcv(n: int = 400, seed: int = 1, drift: float = 0.0008,
                    vol: float = 0.01) -> pd.DataFrame:
    idx = pd.date_range("2022-01-01", periods=n, freq="B")
    rng = np.random.default_rng(seed)
    close = pd.Series(100 * np.exp(np.cumsum(rng.normal(drift, vol, n))), index=idx)
    return pd.DataFrame({"open": close.shift(1).fillna(close.iloc[0]),
                          "high": close * 1.01, "low": close * 0.99,
                          "close": close, "volume": 1_000_000.0}, index=idx)


def _always_long(df: pd.DataFrame) -> pd.Series:
    return pd.Series(1.0, index=df.index)


def _always_short(df: pd.DataFrame) -> pd.Series:
    return pd.Series(-1.0, index=df.index)


def test_runs_end_to_end_and_counts_scenarios():
    df = _trending_ohlcv(seed=1)
    grid = [0.0, 1.0, 2.0]
    cert = certify_robustness(df, _always_long, seed=0, epsilon_grid=grid, trials_per_epsilon=5)
    assert cert.scenarios_run == len(grid) * 5
    assert len(cert.curve) == len(grid)
    assert 0.0 <= cert.baseline_dsr <= 1.0 or np.isnan(cert.baseline_dsr)


def test_strong_positive_drift_survives_stress():
    # Low vol relative to a strong, consistent drift: block reshuffling
    # barely changes the outcome (every block looks similarly "up"), so
    # this should not break anywhere in a modest search range.
    df = _trending_ohlcv(n=500, seed=3, drift=0.0015, vol=0.004)
    cert = certify_robustness(df, _always_long, seed=1,
                              epsilon_grid=[0.0, 1.0, 2.0], trials_per_epsilon=15)
    assert cert.baseline_dsr > 0.5
    assert cert.epsilon_star is None


def test_consistently_wrong_direction_breaks_immediately():
    # Always short on a strongly, consistently upward-drifting series: every
    # resampled scenario is still upward-drifting (block reshuffle preserves
    # the marginal return distribution), so shorting it loses money in
    # essentially every draw -- this should fail at epsilon=0 already.
    df = _trending_ohlcv(n=500, seed=3, drift=0.0015, vol=0.004)
    cert = certify_robustness(df, _always_short, seed=1,
                              epsilon_grid=[0.0, 1.0, 2.0], trials_per_epsilon=15)
    assert cert.baseline_dsr < 0.5
    assert cert.epsilon_star == 0.0


def test_sequencing_sensitive_strategy_has_wide_dsr_spread():
    # Documents the real property the module docstring names: a real
    # crossover strategy's edge over a modest bar count can swing widely
    # across resampled histories of its OWN past, even before any injected
    # stress (epsilon=0) -- this is not asserting a specific certificate
    # value (that would be flaky by the module's own honest admission),
    # only that the per-trial spread this analysis surfaces is real and
    # visible in the returned data, not silently averaged away.
    df = _trending_ohlcv(n=500, seed=42, drift=0.0008, vol=0.012)
    cert = certify_robustness(df, sma_crossover(20, 50), seed=1,
                              epsilon_grid=[0.0], trials_per_epsilon=20)
    samples = cert.curve[0].dsr_samples
    assert len(samples) == 20
    assert max(samples) - min(samples) > 0.2  # a real, wide spread from resampling alone


def test_reproducible_with_same_seed():
    df = _trending_ohlcv(seed=5)
    a = certify_robustness(df, sma_crossover(10, 30), seed=99,
                           epsilon_grid=[0.0, 1.0], trials_per_epsilon=4)
    b = certify_robustness(df, sma_crossover(10, 30), seed=99,
                           epsilon_grid=[0.0, 1.0], trials_per_epsilon=4)
    assert [r.median_dsr for r in a.curve] == [r.median_dsr for r in b.curve]
    assert a.scenarios_run == b.scenarios_run


def test_rejects_too_little_history():
    tiny = _trending_ohlcv(n=10)
    with pytest.raises(ValueError, match="need at least"):
        certify_robustness(tiny, _always_long, block_len=20)


def test_rejects_empty_dataframe():
    with pytest.raises(ValueError, match="no price data"):
        certify_robustness(pd.DataFrame(), _always_long)
