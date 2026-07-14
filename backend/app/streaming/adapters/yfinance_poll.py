"""Yahoo-symbol mapping helper (used by the equity poll + historical backfill).

The streaming adapter that once lived here was part of the 24/7 ticker daemon,
removed in the single-process build; only the pure symbol mapping remains.
"""
from __future__ import annotations

from app.models.enums import AssetClass
from app.streaming.base import Subscription

_COMMODITY_YF = {
    "GOLD": "GC=F", "SILVER": "SI=F", "CRUDE": "CL=F",
    "NATGAS": "NG=F", "COPPER": "HG=F",
}


def to_yahoo_symbol(sub: Subscription) -> str:
    if sub.asset_class == AssetClass.COMMODITY:
        return _COMMODITY_YF.get(sub.symbol.upper(), sub.symbol)
    if sub.asset_class in (AssetClass.IN_EQUITY, AssetClass.IN_INDEX):
        # Indexes already carry a caret (^NSEI); equities get .NS.
        return sub.symbol if sub.symbol.startswith("^") else f"{sub.symbol}.NS"
    return sub.symbol
