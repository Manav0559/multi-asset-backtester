"""Redis tick bus — the fan-out backbone for live market data.

Channel scheme (keep it flat and predictable so the WS hub in Step 4 can
subscribe with pattern matches):
    tick:{EXCHANGE}:{SYMBOL}      e.g. tick:BINANCE:BTCUSDT
    bar:{EXCHANGE}:{SYMBOL}:{TF}  e.g. bar:NASDAQ:AAPL:1m

Adapters publish; the WS hub (and anything else) subscribes. This decouples
the process that ingests a vendor stream from the processes holding browser
WebSocket connections, which is what lets us scale FastAPI horizontally.

Uses redis.asyncio so a single event loop can host many adapters + the hub.
"""
from __future__ import annotations

import redis.asyncio as aioredis

from app.core.config import settings
from app.streaming.envelope import Bar, Tick


def tick_channel(exchange: str, symbol: str) -> str:
    return f"tick:{exchange}:{symbol}"


def bar_channel(exchange: str, symbol: str, timeframe: str) -> str:
    return f"bar:{exchange}:{symbol}:{timeframe}"


class TickBus:
    def __init__(self, redis_url: str | None = None):
        self._url = redis_url or settings.REDIS_URL
        self._redis: aioredis.Redis | None = None

    async def connect(self) -> None:
        if self._redis is None:
            self._redis = aioredis.from_url(self._url, decode_responses=True)
            await self._redis.ping()

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    @property
    def redis(self) -> aioredis.Redis:
        if self._redis is None:
            raise RuntimeError("TickBus not connected; call connect() first")
        return self._redis

    async def publish_tick(self, tick: Tick) -> None:
        await self.redis.publish(tick_channel(tick.exchange, tick.symbol), tick.to_json())

    async def publish_bar(self, bar: Bar) -> None:
        await self.redis.publish(
            bar_channel(bar.exchange, bar.symbol, bar.timeframe.value), bar.to_json()
        )
