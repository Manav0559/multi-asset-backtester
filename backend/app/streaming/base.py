"""Small subscription spec shared by the equity poll + symbol helpers."""
from __future__ import annotations

from dataclasses import dataclass

from app.models.enums import AssetClass, Timeframe


@dataclass(frozen=True)
class Subscription:
    """One instrument to price."""
    symbol: str
    exchange: str
    asset_class: AssetClass
    timeframe: Timeframe = Timeframe.M1
