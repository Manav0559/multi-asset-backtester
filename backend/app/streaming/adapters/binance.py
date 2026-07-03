"""Binance adapter — real-time crypto via combined WebSocket streams.

Subscribes to per-symbol kline (candle) streams and emits a Bar on every
update, flagging is_closed from Binance's `k.x` field so downstream can
distinguish a still-forming live candle from a finalized one (only closed
bars get persisted to ohlcv_bars). Auto-reconnects with capped backoff.

Docs: wss://stream.binance.com:9443/stream?streams=btcusdt@kline_1m/...
"""
from __future__ import annotations

import asyncio
import json
import logging

import websockets

from app.core.config import settings
from app.models.enums import Timeframe
from app.streaming.base import StreamAdapter, Subscription
from app.streaming.bus import TickBus
from app.streaming.envelope import make_bar

logger = logging.getLogger("streaming.binance")

# Binance uses the same interval codes we do for these timeframes.
_TF_TO_BINANCE = {
    Timeframe.M1: "1m", Timeframe.M5: "5m", Timeframe.M15: "15m",
    Timeframe.H1: "1h", Timeframe.D1: "1d",
}


class BinanceAdapter(StreamAdapter):
    name = "binance"

    def __init__(self, subscriptions: list[Subscription], bus: TickBus):
        super().__init__(subscriptions, bus)
        # Binance stream names are lowercase: "btcusdt@kline_1m"
        self._streams = [
            f"{s.symbol.lower()}@kline_{_TF_TO_BINANCE[s.timeframe]}"
            for s in subscriptions
        ]
        # symbol(lower) -> Subscription, to recover asset_class on inbound msgs
        self._by_symbol = {s.symbol.lower(): s for s in subscriptions}

    def _url(self) -> str:
        return f"{settings.BINANCE_WS_URL}?streams={'/'.join(self._streams)}"

    async def run(self) -> None:
        backoff = 1.0
        while not self.stopped:
            try:
                async with websockets.connect(self._url(), ping_interval=20) as ws:
                    logger.info("binance connected: %d streams", len(self._streams))
                    backoff = 1.0  # reset after a healthy connect
                    async for raw in ws:
                        if self.stopped:
                            break
                        await self._handle(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — resilient supervisor loop
                logger.warning("binance stream error: %s; reconnecting in %.1fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _handle(self, raw: str | bytes) -> None:
        msg = json.loads(raw)
        data = msg.get("data", msg)
        k = data.get("k")
        if not k:
            return
        sub = self._by_symbol.get(k["s"].lower())
        if sub is None:
            return
        bar = make_bar(
            symbol=sub.symbol,
            exchange=sub.exchange,
            asset_class=sub.asset_class,
            timeframe=sub.timeframe,
            ts=_ms_to_dt(k["t"]),   # kline open time
            o=k["o"], h=k["h"], l=k["l"], c=k["c"], volume=k["v"],
            trade_count=k.get("n"),
            is_closed=bool(k.get("x", False)),
        )
        await self.bus.publish_bar(bar)


def _ms_to_dt(ms: int):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
