"""Execution cost / slippage model.

A backtest that fills at the exact close with zero cost reports returns
nobody can capture. This model charges three things on every unit of
turnover (|position change|):

  1. Commission — flat bps of notional.
  2. Half-spread — you cross the bid/ask, paying half the quoted spread on
     entry and half on exit; charged per side => on turnover.
  3. Market impact — the square-root law (Almgren, and standard on trading
     desks): impact ≈ eta · sqrt(participation), where
        participation = order_notional / bar_dollar_volume.
     Bigger orders relative to traded volume move the price against you,
     super-linearly. This is the term that punishes illiquid names and
     oversized positions, and separates a realistic backtest from a toy.

Cost is returned in *return space* (fraction of equity) so it plugs directly
into the vectorized engine's net-return calculation.

Approximation (documented): notional uses `capital_base` rather than the
live compounding equity, to avoid a circular dependency (equity depends on
cost depends on equity). For cost estimation this is immaterial.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class CostModel:
    commission_bps: float = 0.0
    spread_bps: float = 0.0        # full quoted spread; half charged per side
    slippage_bps: float = 0.0      # extra fixed slippage per side
    impact_coef: float = 0.0       # eta in the sqrt-impact law (0 disables)
    max_participation: float = 1.0  # cap participation before sqrt (sanity)

    @property
    def linear_bps(self) -> float:
        """Per-turnover linear cost: commission + half-spread + slippage."""
        return self.commission_bps + self.spread_bps / 2.0 + self.slippage_bps

    def per_bar_cost(
        self,
        position: pd.Series,
        close: pd.Series,
        volume: pd.Series,
        capital_base: float,
    ) -> pd.Series:
        """Cost per bar in return space (subtract from gross returns)."""
        turnover = position.diff().abs().fillna(position.abs())

        linear = turnover * (self.linear_bps / 10_000.0)

        if self.impact_coef <= 0.0:
            return linear

        dollar_volume = (close.astype(float) * volume.astype(float)).replace(0.0, np.nan)
        order_notional = turnover * capital_base
        participation = (order_notional / dollar_volume).clip(
            upper=self.max_participation
        ).fillna(0.0)
        # impact fraction applies to the traded turnover
        impact = turnover * self.impact_coef * np.sqrt(participation)
        return linear + impact


# A couple of realistic presets for demos / defaults.
FRICTIONLESS = CostModel()
US_EQUITY_RETAIL = CostModel(commission_bps=0.0, spread_bps=2.0, slippage_bps=1.0,
                             impact_coef=0.1)
NSE_DELIVERY = CostModel(commission_bps=1.0, spread_bps=4.0, slippage_bps=2.0,
                         impact_coef=0.15)
