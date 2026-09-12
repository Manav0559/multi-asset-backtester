"""Robustness Certificates — how much adversarial-but-realistic market
stress a strategy's statistical edge survives, expressed as one number.

A backtest Sharpe/DSR measured on one realized history answers "did this
work on the path that actually happened." It says nothing about how fragile
that result is — whether the edge survives a market that looks like the
real one but isn't identical bar-for-bar. This module answers that second
question directly: it generates realistic-but-different alternate histories
of increasing severity and finds the minimal severity at which the
strategy's edge — measured the same way every other backtest on this
platform is (`app.backtest.engine.run_backtest` + `deflated_sharpe_ratio`,
never a separate, bespoke P&L calculation) — stops looking real.

Realism, defined precisely rather than claimed:
  1. Block bootstrap — the alternate return path is stitched from
     contiguous blocks of the asset's OWN real historical daily returns
     (`block_len` bars at a time), not independently-shuffled bars and not
     a parametric distribution. This preserves local structure (volatility
     clustering, autocorrelation) an i.i.d. resample would destroy, at the
     cost of only ever recombining returns that actually happened, in
     chunks — never inventing a return that couldn't occur.
  2. A bounded jump overlay — on top of the bootstrapped path, a small
     fraction of bars (`jump_prob`) get an extra shock drawn from
     N(0, (epsilon * sigma)^2), where sigma is the asset's own historical
     daily return volatility. `epsilon` is therefore a literal, inspectable
     unit: "epsilon=2" means "occasional extra 2-sigma-scale shocks on top
     of resampled real history," not an abstract, unexplained knob.

What "the edge stops looking real" means, precisely: Deflated Sharpe Ratio
(see metrics.py) is a PROBABILITY in [0, 1] — P(true Sharpe exceeds the
expected-max-under-null for this many trials) — so it cannot literally
"cross zero" the way a raw Sharpe can (an earlier draft of this idea said
exactly that; it doesn't hold up against what DSR actually is). The
correct, dimensionally honest threshold is DSR dropping below 0.5: the
point where it becomes more likely than not that there is NO real edge.
That is the certificate's pass/fail line.

A real, measured property of this method worth stating plainly rather than
tuning away: for a strategy with few trade/crossover events (a 20/50 SMA
crossover over a few hundred bars might turn on a handful of pivots), block
resampling alone — with epsilon=0, no injected shocks at all — can already
swing DSR across a wide range from one random draw to the next, because
reshuffling block order changes exactly when those few pivotal crossovers
land. That is not noise to average away; it IS the finding: a strategy
whose measured edge depends on a few trades landing at specific points in
history is more fragile than its single-history backtest Sharpe suggests,
and `EpsilonResult.dsr_samples` (the full per-trial spread, not just the
median) is the right place to see that, not just `epsilon_star` on its
own. More trials narrows the median's own sampling error (linear runtime
cost, one more `run_backtest` call each) but does not and should not
collapse a strategy's genuine sequencing-sensitivity to a single tidy
number.

Explicitly out of scope for this pass (see the project's own next-steps
notes, not silently dropped): distributing the scenario sweep across
taskflow's worker fleet (this runs single-machine, in-process — the same
`run_backtest` call every other backtest on this platform already makes,
just called many times), and an LLM-proposed search-prior over which
scenario parameters to try next instead of a fixed grid. Both are real
follow-ups, not required for this to already be a genuine, honest
certificate — just for it to search more efficiently at larger scale.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.backtest.engine import run_backtest
from app.backtest.metrics import deflated_sharpe_ratio
from app.backtest.strategies import Strategy

#: DSR is a probability; below this, "no real edge" is more likely than
#: "real edge" — see the module docstring for why this replaces the
#: dimensionally-impossible "DSR crosses zero" framing.
_NO_EDGE_THRESHOLD = 0.5

DEFAULT_EPSILON_GRID: list[float] = [0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0]


@dataclass
class EpsilonResult:
    epsilon: float
    median_dsr: float
    dsr_samples: list[float]


@dataclass
class RobustnessCertificate:
    baseline_sharpe: float
    baseline_dsr: float
    #: None means the strategy's median DSR never dropped below the
    #: no-edge threshold anywhere in `search_range` -- report this as
    #: "robust to at least search_range[1] sigma of injected stress",
    #: not as "infinitely robust."
    epsilon_star: float | None
    dsr_at_epsilon_star: float | None
    search_range: tuple[float, float]
    trials_per_epsilon: int
    scenarios_run: int
    curve: list[EpsilonResult] = field(default_factory=list)


def _block_bootstrap(returns: np.ndarray, block_len: int, length: int,
                      rng: np.random.Generator) -> np.ndarray:
    """Stitches random contiguous blocks of `returns` end to end until the
    output reaches `length`. Every value in the output is a real historical
    return; only the ORDER of blocks is randomized, which is exactly what
    preserves within-block structure while still producing a path that
    didn't literally happen."""
    m = len(returns)
    out = np.empty(length)
    filled = 0
    while filled < length:
        start = int(rng.integers(0, m))
        block = returns[start:start + block_len]
        if len(block) < block_len:  # wrapped past the end of history
            block = np.concatenate([block, returns[: block_len - len(block)]])
        take = min(block_len, length - filled)
        out[filled:filled + take] = block[:take]
        filled += take
    return out


def _jump_overlay(returns: np.ndarray, epsilon: float, daily_sigma: float,
                   jump_prob: float, rng: np.random.Generator) -> np.ndarray:
    if epsilon <= 0.0:
        return returns
    out = returns.copy()
    hit = rng.random(len(returns)) < jump_prob
    n_hit = int(hit.sum())
    if n_hit:
        out[hit] += rng.normal(0.0, epsilon * daily_sigma, n_hit)
    return out


def _synthetic_ohlcv(close_path: np.ndarray, index: pd.DatetimeIndex) -> pd.DataFrame:
    """Same synthetic-OHLCV shape the test suite already uses for
    engine-level tests (open = prior close, high/low a fixed +-1% band,
    constant volume) -- run_backtest only reads close/volume, and any
    strategy in BUILTIN_STRATEGIES that touches open/high/low is a chart
    indicator concern, not this module's."""
    close = pd.Series(close_path, index=index)
    return pd.DataFrame({
        "open": close.shift(1).fillna(close.iloc[0]),
        "high": close * 1.01,
        "low": close * 0.99,
        "close": close,
        "volume": 1_000_000.0,
    }, index=index)


def certify_robustness(
    df: pd.DataFrame,
    strategy: Strategy,
    *,
    n_trials: int = 1,
    initial_capital: float = 100_000.0,
    commission_bps: float = 0.0,
    epsilon_grid: list[float] | None = None,
    trials_per_epsilon: int = 20,
    block_len: int = 20,
    jump_prob: float = 0.05,
    seed: int = 0,
) -> RobustnessCertificate:
    """Finds the minimal `epsilon` (see module docstring) at which this
    strategy's median Deflated Sharpe across `trials_per_epsilon`
    independently-seeded stressed scenarios drops below 0.5.

    `n_trials` is passed straight through to `deflated_sharpe_ratio` for
    every scenario, matching whatever trial count the strategy's own
    baseline backtest used — a certificate computed against a different
    (e.g. always-1) trial count than the strategy's reported DSR would be
    comparing two different statistics.
    """
    if df.empty:
        raise ValueError("no price data to stress-test")
    df = df.sort_index()
    close = df["close"].astype(float)
    hist_returns = close.pct_change().dropna().to_numpy()
    if len(hist_returns) < max(20, block_len):
        raise ValueError(
            f"need at least {max(20, block_len)} bars of history to stress-test "
            f"(block_len={block_len}), got {len(hist_returns)}"
        )
    daily_sigma = float(hist_returns.std(ddof=1))
    n = len(hist_returns)
    block_len = min(block_len, max(2, n // 4))

    baseline = run_backtest(df, strategy, initial_capital=initial_capital,
                             commission_bps=commission_bps)
    baseline_std = float(baseline.returns.std(ddof=1))
    baseline_sharpe = (float(baseline.returns.mean() / baseline_std)
                       if baseline_std > 0 else 0.0)
    baseline_dsr = deflated_sharpe_ratio(baseline.returns, n_trials)

    grid = sorted(epsilon_grid if epsilon_grid is not None else DEFAULT_EPSILON_GRID)
    rng = np.random.default_rng(seed)

    epsilon_star: float | None = None
    dsr_at_star: float | None = None
    scenarios_run = 0
    curve: list[EpsilonResult] = []

    for epsilon in grid:
        dsr_samples: list[float] = []
        for _ in range(trials_per_epsilon):
            scenario_returns = _block_bootstrap(hist_returns, block_len, n, rng)
            scenario_returns = _jump_overlay(scenario_returns, epsilon, daily_sigma, jump_prob, rng)
            close_path = close.iloc[0] * np.concatenate([[1.0], np.cumprod(1.0 + scenario_returns)])
            scenario_df = _synthetic_ohlcv(close_path, df.index)
            out = run_backtest(scenario_df, strategy, initial_capital=initial_capital,
                                commission_bps=commission_bps)
            scenarios_run += 1
            dsr_samples.append(deflated_sharpe_ratio(out.returns, n_trials))

        median_dsr = float(np.nanmedian(dsr_samples))
        curve.append(EpsilonResult(epsilon=epsilon, median_dsr=median_dsr, dsr_samples=dsr_samples))
        if epsilon_star is None and median_dsr < _NO_EDGE_THRESHOLD:
            epsilon_star = epsilon
            dsr_at_star = median_dsr

    return RobustnessCertificate(
        baseline_sharpe=baseline_sharpe,
        baseline_dsr=baseline_dsr,
        epsilon_star=epsilon_star,
        dsr_at_epsilon_star=dsr_at_star,
        search_range=(grid[0], grid[-1]),
        trials_per_epsilon=trials_per_epsilon,
        scenarios_run=scenarios_run,
        curve=curve,
    )
