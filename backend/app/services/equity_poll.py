"""Delayed equity price poll — the honest non-crypto feed.

yfinance free-tier equity quotes are vendor-DELAYED (typically ~15 min), so
this is NOT a live feed and is badged DELAYED everywhere it surfaces. A beat
task polls a small liquid set on a modest cadence, only while the market is
open, and publishes ticks onto the SAME tick:{exchange}:{symbol} channel the
crypto path uses — so the chart code is identical; only the provenance badge
differs.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.models import Asset
from app.models.enums import AssetClass
from app.services.market_hours import market_status
from app.streaming.adapters.yfinance_poll import to_yahoo_symbol
from app.streaming.base import Subscription
from app.streaming.inproc_bus import bus

logger = logging.getLogger("equity.poll")


def tick_channel(exchange: str, symbol: str) -> str:
    return f"tick:{exchange}:{symbol}"

# Liquid megacaps that reliably resolve on Yahoo — bounded to keep us well under
# any rate limit even at a fast cadence.
_POLL = [("AAPL", "NASDAQ", AssetClass.US_EQUITY),
         ("MSFT", "NASDAQ", AssetClass.US_EQUITY),
         ("NVDA", "NASDAQ", AssetClass.US_EQUITY),
         ("RELIANCE", "NSE", AssetClass.IN_EQUITY),
         ("TCS", "NSE", AssetClass.IN_EQUITY)]

_SNAPSHOT_TTL_S = 30


def _latest_price(symbol: str, exchange: str, asset_class: AssetClass) -> float | None:
    import yfinance as yf
    yf_sym = to_yahoo_symbol(Subscription(symbol, exchange, asset_class))
    try:
        fi = yf.Ticker(yf_sym).fast_info
        px = fi.get("lastPrice") or fi.get("last_price")
        return float(px) if px else None
    except Exception:  # noqa: BLE001 — vendor hiccup; skip this symbol this tick
        return None


def poll_equity_ticks(db) -> int:
    """Publish one delayed tick per open-market equity in the poll set onto the
    in-process bus (dashboards subscribed to tick:{exchange}:{symbol} update
    while the market is open). Returns the number of ticks published."""
    published = 0
    for symbol, exchange, asset_class in _POLL:
        if market_status(exchange, asset_class)["is_open"] is not True:
            continue  # closed or unknown — the UI shows the last session instead
        # Only publish for assets we actually track (so the frontend can map it).
        exists = db.scalar(select(Asset.id).where(Asset.symbol == symbol,
                                                  Asset.exchange == exchange))
        if not exists:
            continue
        price = _latest_price(symbol, exchange, asset_class)
        if price is None:
            continue
        bus.publish(tick_channel(exchange, symbol), {
            "symbol": symbol, "exchange": exchange, "asset_class": asset_class.value,
            "price": str(price), "volume": "0",
            "ts": datetime.now(timezone.utc).isoformat(),
            "delayed": True,  # provenance: never presented as live
        })
        published += 1
    return published
