"""yfinance polling adapter — NSE equities/indexes + commodities.

Phase-1 free tier: there is no free NSE/commodity WebSocket, so we poll
Yahoo Finance on an interval and emit a Tick per symbol per cycle. yfinance
is blocking, so each poll runs in a thread via asyncio.to_thread to avoid
stalling the event loop.

Symbol mapping is Yahoo-specific and lives ONLY here (the canonical symbol
stays clean upstream): NSE equities use a ".NS" suffix, commodities use
Yahoo futures tickers (GC=F gold, SI=F silver, CL=F crude).

Designed so swapping to Zerodha Kite (Phase 2) means adding a KiteAdapter,
not touching anything downstream.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.core.config import settings
from app.models.enums import AssetClass
from app.streaming.base import StreamAdapter, Subscription
from app.streaming.bus import TickBus
from app.streaming.envelope import make_tick

logger = logging.getLogger("streaming.yfinance")

# Canonical commodity symbol -> Yahoo futures ticker.
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


class YFinanceAdapter(StreamAdapter):
    name = "yfinance"

    def __init__(self, subscriptions: list[Subscription], bus: TickBus,
                 poll_seconds: float | None = None):
        super().__init__(subscriptions, bus)
        self._interval = poll_seconds or settings.YFINANCE_POLL_SECONDS
        self._yf_map = {to_yahoo_symbol(s): s for s in subscriptions}

    async def run(self) -> None:
        while not self.stopped:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("yfinance poll error: %s", exc)
            await asyncio.sleep(self._interval)

    async def _poll_once(self) -> None:
        quotes = await asyncio.to_thread(self._fetch, list(self._yf_map.keys()))
        for yf_symbol, (price, volume, ts) in quotes.items():
            sub = self._yf_map[yf_symbol]
            if price is None:
                continue
            await self.bus.publish_tick(make_tick(
                symbol=sub.symbol, exchange=sub.exchange, asset_class=sub.asset_class,
                price=price, volume=volume or 0, ts=ts or datetime.now(timezone.utc),
            ))

    @staticmethod
    def _fetch(yf_symbols: list[str]) -> dict:
        """Blocking Yahoo fetch. Isolated + import-local so the module
        imports even when yfinance isn't installed (tests stub this)."""
        import yfinance as yf

        out: dict = {}
        data = yf.download(tickers=" ".join(yf_symbols), period="1d",
                           interval="1m", progress=False, group_by="ticker",
                           threads=True)
        for sym in yf_symbols:
            try:
                frame = data[sym] if len(yf_symbols) > 1 else data
                last = frame.dropna().iloc[-1]
                ts = frame.dropna().index[-1].to_pydatetime()
                out[sym] = (float(last["Close"]), float(last.get("Volume", 0)), ts)
            except Exception:  # noqa: BLE001 — a single bad symbol shouldn't kill the cycle
                out[sym] = (None, None, None)
        return out
