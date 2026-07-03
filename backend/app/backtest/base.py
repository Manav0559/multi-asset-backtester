"""BaseStrategy — the ONE strategy contract every algorithm implements.

The signal contract (fixed platform-wide): a strategy outputs
TARGET WEIGHTS per asset per bar. `>0` long, `<0` short, `sum(|w|)` = gross
leverage, `sum(w)` = net exposure.

  kind="single"    generate(df)     -> pd.Series   (OHLCV frame in, weight per bar;
                                       {0,1} is the long/flat special case)
  kind="portfolio" generate(prices) -> pd.DataFrame (close panel in, weight per
                                       asset per bar)

Strategies must be strictly backward-looking. The engine enforces
no-lookahead by shifting signals one bar before they take effect, but a
strategy must not peek internally (no .shift(-1), no future indexing).

Instances are callables, so they slot directly into both engines and stay
drop-in compatible with the plain-function strategies that predate this class.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd


class BaseStrategy(ABC):
    key: str = ""                # registry identifier, e.g. "momentum_breakout"
    description: str = ""
    category: str = "custom"     # trend | mean_reversion | arbitrage | baseline | ml | custom
    kind: str = "single"         # "single" | "portfolio"

    def __init__(self, **params):
        self.params = params

    @abstractmethod
    def generate(self, data: pd.DataFrame) -> pd.Series | pd.DataFrame:
        """Target weights for `data`. Must be index-aligned with the input."""

    def __call__(self, data: pd.DataFrame) -> pd.Series | pd.DataFrame:
        return self.generate(data)
