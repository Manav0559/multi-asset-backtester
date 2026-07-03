"""StrategyRegistry + classic algos + BYOC sandbox tests.

Registry: every legacy strategy is reachable under its old key with the right
kind, and classic BaseStrategy classes carry their own metadata. Classics: no
lookahead (weights at t must not change when future bars are appended) and
sane output ranges. Sandbox: the template runs end-to-end; imports, dunder
access, and lookahead-y APIs are rejected with line numbers; runtime errors
surface as SandboxError, not worker crashes.
"""
import numpy as np
import pandas as pd
import pytest

from app.backtest.classic import BollingerReversion, MacdTrend, MomentumBreakout, PairsTrading
from app.backtest.registry import STRATEGY_REGISTRY
from app.backtest.sandbox import (
    DEFAULT_TEMPLATE,
    SandboxError,
    build_custom_strategy,
    run_custom_strategy,
    validate_code,
)


def _ohlcv(n: int = 200, seed: int = 3) -> pd.DataFrame:
    idx = pd.date_range("2023-01-01", periods=n, freq="D", tz="UTC")
    rng = np.random.default_rng(seed)
    close = pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0005, 0.02, n))), index=idx)
    return pd.DataFrame({"open": close.shift(1).fillna(close.iloc[0]),
                         "high": close * 1.01, "low": close * 0.99,
                         "close": close, "volume": rng.uniform(1e5, 1e6, n)}, index=idx)


# ------------------------------------------------------------- registry --
def test_registry_covers_legacy_and_classic():
    keys = {e["key"]: e for e in STRATEGY_REGISTRY.catalog()}
    # legacy singles
    for k in ("sma_crossover", "rsi_reversion", "buy_and_hold", "ml_direction"):
        assert keys[k]["kind"] == "single"
    # legacy portfolio
    for k in ("cross_sectional_momentum", "long_short_sma", "equal_weight_long"):
        assert keys[k]["kind"] == "portfolio"
    # classics carry metadata + defaults from their signatures
    assert keys["momentum_breakout"]["kind"] == "single"
    assert keys["momentum_breakout"]["defaults"]["entry"] == 55
    assert keys["pairs_trading"]["kind"] == "portfolio"
    assert keys["pairs_trading"]["category"] == "arbitrage"


def test_registry_build_matches_direct_call():
    df = _ohlcv()
    via_registry = STRATEGY_REGISTRY.build("momentum_breakout", {"entry": 20, "exit_": 10})(df)
    direct = MomentumBreakout(entry=20, exit_=10)(df)
    pd.testing.assert_series_equal(via_registry, direct)


# -------------------------------------------------------------- classics --
@pytest.mark.parametrize("cls,params", [
    (MomentumBreakout, {"entry": 20, "exit_": 10}),
    (BollingerReversion, {"length": 20}),
    (MacdTrend, {}),
])
def test_classic_single_no_lookahead(cls, params):
    """Weights on the shared prefix must be identical with/without the future
    appended — the platform's core no-lookahead invariant."""
    df = _ohlcv(200)
    full = cls(**params)(df)
    truncated = cls(**params)(df.iloc[:150])
    pd.testing.assert_series_equal(full.iloc[:150], truncated, check_names=False)
    assert set(np.unique(full)) <= {0.0, 1.0}


def test_pairs_trading_market_neutral():
    n = 300
    idx = pd.date_range("2023-01-01", periods=n, freq="D", tz="UTC")
    rng = np.random.default_rng(11)
    base = np.cumsum(rng.normal(0, 0.01, n))
    a = 100 * np.exp(base + rng.normal(0, 0.02, n))          # noisy around the pair
    b = 100 * np.exp(base)
    prices = pd.DataFrame({"1": a, "2": b}, index=idx)
    w = PairsTrading(lookback=40)(prices)
    assert (w.sum(axis=1).abs() < 1e-12).all()               # dollar-neutral every bar
    assert (w.abs().sum(axis=1) > 0).any()                    # actually trades
    with pytest.raises(ValueError, match="exactly 2"):
        PairsTrading()(prices.assign(third=b))


# --------------------------------------------------------------- sandbox --
def test_template_runs_end_to_end():
    strategy = build_custom_strategy(DEFAULT_TEMPLATE)
    weights = run_custom_strategy(strategy, _ohlcv())
    assert isinstance(weights, pd.Series)
    assert weights.between(-1, 1).all()
    assert (weights != 0).any()             # the golden cross fires somewhere


def test_sandbox_rejects_imports_and_dunders():
    assert any("imports are not allowed" in e for e in validate_code("import os"))
    assert any("not allowed" in e for e in
               validate_code("x = ().__class__.__bases__"))
    assert any("'eval' is not allowed" in e for e in validate_code("eval('1')"))
    assert any("line 1" in e for e in validate_code("def broken(:"))


def test_sandbox_requires_exactly_one_class():
    with pytest.raises(SandboxError, match="exactly one class"):
        build_custom_strategy("x = 1")


def test_sandbox_runtime_errors_are_contained():
    src = (
        "class Boom(CustomStrategy):\n"
        "    def next(self, i, bar):\n"
        "        raise ValueError('user bug')\n"
    )
    strategy = build_custom_strategy(src)
    with pytest.raises(SandboxError, match="user bug"):
        run_custom_strategy(strategy, _ohlcv(30))


def test_sandbox_output_clipped_and_aligned():
    src = (
        "class Lever(CustomStrategy):\n"
        "    def generate(self, data):\n"
        "        return pd.Series(5.0, index=data.index)\n"   # tries 5x leverage
    )
    weights = run_custom_strategy(build_custom_strategy(src), _ohlcv(30))
    assert (weights == 1.0).all()           # clipped to the contract


def test_sandbox_vectorized_generate_supported():
    src = (
        "class Vec(CustomStrategy):\n"
        "    params = {'fast': 5, 'slow': 15}\n"
        "    def generate(self, data):\n"
        "        close = data['close']\n"
        "        fast = close.rolling(self.params['fast']).mean()\n"
        "        slow = close.rolling(self.params['slow']).mean()\n"
        "        return (fast > slow).astype(float)\n"
    )
    weights = run_custom_strategy(build_custom_strategy(src, {"fast": 3}), _ohlcv(60))
    assert set(np.unique(weights)) <= {0.0, 1.0}
