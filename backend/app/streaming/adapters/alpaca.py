"""Alpaca adapter — US equities real-time via WebSocket.

Connects to Alpaca's market-data stream, authenticates, subscribes to the
requested symbols' bar channel, and emits closed 1-minute Bars. Falls back
cleanly if credentials are absent (logs and idles rather than crashing the
supervisor), since the free tier requires keys.

Feed: wss://stream.data.alpaca.markets/v2/{iex|sip}
Protocol: JSON arrays of messages; auth -> subscribe -> stream ("b" = bar).
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

import websockets

from app.core.config import settings
from app.models.enums import Timeframe
from app.streaming.base import StreamAdapter, Subscription
from app.streaming.bus import TickBus
from app.streaming.envelope import make_bar

logger = logging.getLogger("streaming.alpaca")


class AlpacaAdapter(StreamAdapter):
    name = "alpaca"

    def __init__(self, subscriptions: list[Subscription], bus: TickBus):
        super().__init__(subscriptions, bus)
        self._by_symbol = {s.symbol.upper(): s for s in subscriptions}

    def _url(self) -> str:
        return f"wss://stream.data.alpaca.markets/v2/{settings.ALPACA_FEED}"

    async def run(self) -> None:
        if not settings.ALPACA_API_KEY or not settings.ALPACA_API_SECRET:
            logger.warning("alpaca credentials missing; adapter idle")
            await self._stopped.wait()
            return

        backoff = 1.0
        while not self.stopped:
            try:
                async with websockets.connect(self._url(), ping_interval=20) as ws:
                    await self._authenticate(ws)
                    await self._subscribe(ws)
                    logger.info("alpaca connected: %d symbols", len(self._by_symbol))
                    backoff = 1.0
                    async for raw in ws:
                        if self.stopped:
                            break
                        await self._handle(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("alpaca stream error: %s; reconnecting in %.1fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _authenticate(self, ws) -> None:
        await ws.send(json.dumps({
            "action": "auth",
            "key": settings.ALPACA_API_KEY,
            "secret": settings.ALPACA_API_SECRET,
        }))

    async def _subscribe(self, ws) -> None:
        await ws.send(json.dumps({
            "action": "subscribe",
            "bars": list(self._by_symbol.keys()),
        }))

    async def _handle(self, raw: str | bytes) -> None:
        for msg in json.loads(raw):
            if msg.get("T") != "b":  # only minute bars
                continue
            sub = self._by_symbol.get(msg["S"].upper())
            if sub is None:
                continue
            bar = make_bar(
                symbol=sub.symbol, exchange=sub.exchange, asset_class=sub.asset_class,
                timeframe=Timeframe.M1,
                ts=datetime.fromisoformat(msg["t"].replace("Z", "+00:00")),
                o=msg["o"], h=msg["h"], l=msg["l"], c=msg["c"], volume=msg["v"],
                trade_count=msg.get("n"), vwap=msg.get("vw"),
                is_closed=True,  # Alpaca "b" messages are finalized minute bars
            )
            await self.bus.publish_bar(bar)
