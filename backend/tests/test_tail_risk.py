"""Tail-risk metrics: VaR/ES/Cornish-Fisher validated against known
distributions, not just smoke-tested."""
import numpy as np
import pandas as pd

from app.backtest.metrics import tail_risk_metrics


def _series(x):
    return pd.Series(x, index=pd.date_range("2024-01-01", periods=len(x), freq="D"))


def test_normal_returns_match_analytic_var():
    """On N(mu, sigma): VaR95 ≈ -(mu - 1.645σ), CF reduces to normal (S=0,K=0),
    ES95 ≈ (φ(z)/0.05)σ - mu."""
    rng = np.random.default_rng(7)
    mu, sd = 0.0005, 0.01
    r = _series(rng.normal(mu, sd, 100_000))
    m = tail_risk_metrics(r)
    assert abs(m["var_95"] - (1.6449 * sd - mu)) < 0.0005
    # CF correction vanishes for a normal: matches historical within noise.
    assert abs(m["cf_var_95"] - m["var_95"]) < 0.0005
    # Analytic normal ES95 = sigma * phi(z_95)/0.05 - mu = sigma*2.0627 - mu
    assert abs(m["es_95"] - (2.0627 * sd - mu)) < 0.0008
    assert abs(m["skew"]) < 0.05 and abs(m["excess_kurtosis"]) < 0.1


def test_orderings_hold():
    """ES >= VaR at the same level; 99% >= 95%; all positive for a loss-making
    tail. These orderings are definitional — violating any is a bug."""
    rng = np.random.default_rng(11)
    r = _series(rng.standard_t(df=4, size=50_000) * 0.01)  # fat tails
    m = tail_risk_metrics(r)
    assert m["es_95"] >= m["var_95"] > 0
    assert m["es_99"] >= m["var_99"] >= m["var_95"]
    assert m["es_99"] >= m["es_95"]


def test_negative_skew_pushes_cf_var_beyond_normal():
    """The honest direction: a left-skewed strategy must show CF-VaR above the
    symmetric-normal estimate at 99% (that's what the correction is FOR)."""
    rng = np.random.default_rng(13)
    base = rng.normal(0, 0.008, 50_000)
    crashes = rng.choice([0.0, -0.05], size=50_000, p=[0.995, 0.005])
    r = _series(base + crashes)
    m = tail_risk_metrics(r)
    assert m["skew"] < -0.5
    normal_var_99 = 2.3263 * float(r.std(ddof=1)) - float(r.mean())
    assert m["cf_var_99"] > normal_var_99


def test_short_series_returns_empty():
    assert tail_risk_metrics(_series(np.zeros(5))) == {}
