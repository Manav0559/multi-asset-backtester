"""LeakGuard: static lookahead-bias detection layered on the BYOC sandbox.

Each known pattern (negative shift, centered rolling, backward-fill, direct
forward indexing in next()/on_bar()) is checked in isolation via
detect_lookahead(), then once end-to-end through validate_code() and
build_custom_strategy() to confirm the merge into the existing sandbox gate
actually rejects submission — plus negative controls: the shipped
DEFAULT_TEMPLATE and other legitimate causal patterns (positive shift,
non-centered rolling, ffill, in-bounds indexing) must never be flagged.
"""
import numpy as np
import pandas as pd
import pytest

from app.backtest.leakguard import detect_lookahead
from app.backtest.sandbox import DEFAULT_TEMPLATE, SandboxError, build_custom_strategy, validate_code


def _ohlcv(n: int = 60, seed: int = 7) -> pd.DataFrame:
    idx = pd.date_range("2023-01-01", periods=n, freq="D", tz="UTC")
    rng = np.random.default_rng(seed)
    close = pd.Series(100 * np.exp(np.cumsum(rng.normal(0.0005, 0.02, n))), index=idx)
    return pd.DataFrame({"open": close.shift(1).fillna(close.iloc[0]),
                          "high": close * 1.01, "low": close * 0.99,
                          "close": close, "volume": 1_000_000}, index=idx)


# --------------------------------------------------------- proven patterns --
def test_negative_shift_flagged():
    src = (
        "class Peek(CustomStrategy):\n"
        "    def setup(self):\n"
        "        self.fast = self.data['close'].shift(-3)\n"
        "    def next(self, i, bar):\n"
        "        return 1.0 if self.fast.iloc[i] > bar['close'] else 0.0\n"
    )
    findings = detect_lookahead(src)
    assert any("shift() with a negative argument" in f for f in findings)
    assert any("line 3" in f for f in findings)


def test_shift_zero_and_positive_not_flagged():
    src = (
        "class Fine(CustomStrategy):\n"
        "    def setup(self):\n"
        "        self.a = self.data['close'].shift(1)\n"
        "        self.b = self.data['close'].shift(0)\n"
        "        self.c = self.data['close'].shift()\n"
        "    def next(self, i, bar):\n"
        "        return 0.0\n"
    )
    assert detect_lookahead(src) == []


def test_centered_rolling_flagged():
    src = (
        "class Centered(CustomStrategy):\n"
        "    def setup(self):\n"
        "        self.avg = self.data['close'].rolling(10, center=True).mean()\n"
        "    def next(self, i, bar):\n"
        "        return 0.0\n"
    )
    findings = detect_lookahead(src)
    assert any("rolling(..., center=True)" in f for f in findings)


def test_rolling_center_false_not_flagged():
    src = (
        "class Fine(CustomStrategy):\n"
        "    def setup(self):\n"
        "        self.avg = self.data['close'].rolling(10, center=False).mean()\n"
        "    def next(self, i, bar):\n"
        "        return 0.0\n"
    )
    assert detect_lookahead(src) == []


def test_bfill_method_flagged():
    src = (
        "class Backfilled(CustomStrategy):\n"
        "    def setup(self):\n"
        "        self.a = self.data['close'].bfill()\n"
        "        self.b = self.data['close'].fillna(method='bfill')\n"
        "    def next(self, i, bar):\n"
        "        return 0.0\n"
    )
    findings = detect_lookahead(src)
    assert sum("future value" in f for f in findings) == 2


def test_ffill_not_flagged():
    src = (
        "class Fine(CustomStrategy):\n"
        "    def setup(self):\n"
        "        self.a = self.data['close'].ffill()\n"
        "        self.b = self.data['close'].fillna(method='ffill')\n"
        "    def next(self, i, bar):\n"
        "        return 0.0\n"
    )
    assert detect_lookahead(src) == []


def test_forward_indexing_in_next_flagged():
    src = (
        "class Cheater(CustomStrategy):\n"
        "    def next(self, i, bar):\n"
        "        future = self.data['close'].iloc[i + 1]\n"
        "        return 1.0 if future > bar['close'] else 0.0\n"
    )
    findings = detect_lookahead(src)
    assert any("indexing 1 bar(s) ahead" in f for f in findings)
    assert any("line 3" in f for f in findings)


def test_backward_and_current_indexing_in_next_not_flagged():
    src = (
        "class Fine(CustomStrategy):\n"
        "    def setup(self):\n"
        "        self.avg = self.data['close'].rolling(5).mean()\n"
        "    def next(self, i, bar):\n"
        "        past = self.data['close'].iloc[i - 1]\n"
        "        cur = self.avg.iloc[i]\n"
        "        return 1.0 if cur > past else 0.0\n"
    )
    assert detect_lookahead(src) == []


def test_index_offset_outside_next_not_flagged():
    # `i + 1` means nothing special outside next()/on_bar() -- e.g. an
    # ordinary loop variable in setup() must not trip the forward-indexing
    # heuristic just because it happens to be named `i`.
    src = (
        "class Fine(CustomStrategy):\n"
        "    def setup(self):\n"
        "        vals = [0.0] * len(self.data)\n"
        "        for i in range(len(self.data) - 1):\n"
        "            vals[i] = self.data['close'].iloc[i + 1] - self.data['close'].iloc[i]\n"
        "        self.diff = pd.Series(vals, index=self.data.index)\n"
        "    def next(self, i, bar):\n"
        "        return 0.0\n"
    )
    assert detect_lookahead(src) == []


# ------------------------------------------------ end-to-end integration --
def test_shipped_template_has_no_lookahead_findings():
    assert detect_lookahead(DEFAULT_TEMPLATE) == []
    assert validate_code(DEFAULT_TEMPLATE) == []


def test_validate_code_merges_leakguard_findings():
    src = "class Peek(CustomStrategy):\n    def next(self, i, bar):\n        return self.data['close'].iloc[i + 2]\n"
    errors = validate_code(src)
    assert any("indexing 2 bar(s) ahead" in e for e in errors)


def test_build_custom_strategy_rejects_proven_lookahead():
    src = (
        "class Peek(CustomStrategy):\n"
        "    def setup(self):\n"
        "        self.fast = self.data['close'].shift(-2)\n"
        "    def next(self, i, bar):\n"
        "        return 1.0 if self.fast.iloc[i] > bar['close'] else 0.0\n"
    )
    with pytest.raises(SandboxError, match="future bars"):
        build_custom_strategy(src)


def test_unrecognized_pattern_is_not_claimed_safe_but_also_not_flagged():
    # apply() with an arbitrary lambda is explicitly out of scope (see the
    # module docstring) -- this just documents the current boundary rather
    # than asserting anything is actually safe.
    src = (
        "class Unknown(CustomStrategy):\n"
        "    def setup(self):\n"
        "        self.x = self.data['close'].apply(lambda v: v * 2)\n"
        "    def next(self, i, bar):\n"
        "        return 0.0\n"
    )
    assert detect_lookahead(src) == []
