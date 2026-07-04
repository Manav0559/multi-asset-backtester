"""Binance adapter — real-time crypto via combined WebSocket streams.

Subscribes, per symbol, to three streams and routes each inbound frame by its
`stream` suffix:
  * @kline_{tf}     -> Bar   (is_closed from `k.x`; only closed bars persist)
  * @trade          -> Tick  (every executed trade — sub-second last price)
  * @depth20@100ms  -> Depth (top-20 L2 book snapshot, ~10/s)

This is the ONLY surface the platform labels LIVE (real exchange feed).
Auto-reconnects with capped backoff.

Docs: wss://stream.binance.com:9443/stream?streams=btcusdt@trade/btcusdt@depth20@100ms/btcusdt@kline_1m
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import websockets

from app.core.config import settings
from app.models.enums import Timeframe
from app.streaming.base import StreamAdapter, Subscription
from app.streaming.bus import TickBus
from app.streaming.envelope import make_bar, make_depth, make_tick

logger = logging.getLogger("streaming.binance")

_TF_TO_BINANCE = {
    Timeframe.M1: "1m", Timeframe.M5: "5m", Timeframe.M15: "15m",
    Timeframe.H1: "1h", Timeframe.D1: "1d",
}


class BinanceAdapter(StreamAdapter):
    name = "binance"

    def __init__(self, subscriptions: list[Subscription], bus: TickBus):
        super().__init__(subscriptions, bus)
        streams: list[str] = []
        for s in subscriptions:
            lower = s.symbol.lower()
            streams.append(f"{lower}@kline_{_TF_TO_BINANCE[s.timeframe]}")
            streams.append(f"{lower}@trade")
            streams.append(f"{lower}@depth20@100ms")
        self._streams = streams
        self._by_symbol = {s.symbol.lower(): s for s in subscriptions}

    def _url(self) -> str:
        return f"{settings.BINANCE_WS_URL}?streams={'/'.join(self._streams)}"

    async def run(self) -> None:
        backoff = 1.0
        while not self.stopped:
            try:
                async with websockets.connect(self._url(), ping_interval=20,
                                              max_queue=256) as ws:
                    logger.info("binance connected: %d streams", len(self._streams))
                    backoff = 1.0
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
        stream = msg.get("stream", "")
        data = msg.get("data", msg)
        sym = stream.split("@", 1)[0]
        sub = self._by_symbol.get(sym)
        if sub is None:
            return
        if "@kline_" in stream:
            await self._on_kline(sub, data)
        elif stream.endswith("@trade"):
            await self._on_trade(sub, data)
        elif "@depth" in stream:
            await self._on_depth(sub, data)

    async def _on_kline(self, sub: Subscription, data: dict) -> None:
        k = data.get("k")
        if not k:
            return
        await self.bus.publish_bar(make_bar(
            symbol=sub.symbol, exchange=sub.exchange, asset_class=sub.asset_class,
            timeframe=sub.timeframe, ts=_ms(k["t"]),
            o=k["o"], h=k["h"], l=k["l"], c=k["c"], volume=k["v"],
            trade_count=k.get("n"), is_closed=bool(k.get("x", False)),
        ))

    async def _on_trade(self, sub: Subscription, data: dict) -> None:
        await self.bus.publish_tick(make_tick(
            symbol=sub.symbol, exchange=sub.exchange, asset_class=sub.asset_class,
            price=data["p"], volume=data.get("q", 0), ts=_ms(data.get("T", data.get("E"))),
        ))

    async def _on_depth(self, sub: Subscription, data: dict) -> None:
        bids = data.get("bids") or data.get("b") or []
        asks = data.get("asks") or data.get("a") or []
        if not bids and not asks:
            return
        await self.bus.publish_depth(make_depth(
            symbol=sub.symbol, exchange=sub.exchange, asset_class=sub.asset_class,
            ts=datetime.now(timezone.utc),
            bids=[[b[0], b[1]] for b in bids[:20]],
            asks=[[a[0], a[1]] for a in asks[:20]],
            is_live=True,
        ))


def _ms(ms: int | None):
    if ms is None:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
