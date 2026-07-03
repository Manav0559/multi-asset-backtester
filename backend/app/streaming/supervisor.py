"""Streaming supervisor — owns the tick bus, the adapters, and the bar
persister, and runs them as a supervised set of asyncio tasks.

Given a list of Subscriptions spanning multiple exchanges, it groups them
by source, instantiates the right adapter for each, and launches everything
on one event loop. Cancellation propagates cleanly to every task.

This is the process entrypoint for `python -m app.streaming.supervisor`
(a standalone ingest worker, separate from the FastAPI web process).
"""
from __future__ import annotations

import asyncio
import logging

from app.models.enums import AssetClass
from app.streaming.adapters.alpaca import AlpacaAdapter
from app.streaming.adapters.binance import BinanceAdapter
from app.streaming.adapters.yfinance_poll import YFinanceAdapter
from app.streaming.base import StreamAdapter, Subscription
from app.streaming.bus import TickBus
from app.streaming.persistence import BarPersister

logger = logging.getLogger("streaming.supervisor")

# Which adapter handles which asset class.
_CRYPTO = {AssetClass.CRYPTO}
_US = {AssetClass.US_EQUITY}
_YF = {AssetClass.IN_EQUITY, AssetClass.IN_INDEX, AssetClass.COMMODITY}


def build_adapters(subs: list[Subscription], bus: TickBus) -> list[StreamAdapter]:
    crypto = [s for s in subs if s.asset_class in _CRYPTO]
    us = [s for s in subs if s.asset_class in _US]
    yf = [s for s in subs if s.asset_class in _YF]

    adapters: list[StreamAdapter] = []
    if crypto:
        adapters.append(BinanceAdapter(crypto, bus))
    if us:
        adapters.append(AlpacaAdapter(us, bus))
    if yf:
        adapters.append(YFinanceAdapter(yf, bus))
    return adapters


class StreamSupervisor:
    def __init__(self, subscriptions: list[Subscription]):
        self.subscriptions = subscriptions
        self.bus = TickBus()
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        await self.bus.connect()
        adapters = build_adapters(self.subscriptions, self.bus)
        persister = BarPersister(self.bus)

        self._tasks = [asyncio.create_task(a.run(), name=f"adapter:{a.name}")
                       for a in adapters]
        self._tasks.append(asyncio.create_task(persister.run(), name="persister"))
        logger.info("supervisor started: %d adapters + persister", len(adapters))

    async def run_forever(self) -> None:
        await self.start()
        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.bus.close()
        logger.info("supervisor stopped")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    # Demo universe. In production this comes from the `assets` table.
    demo = [
        Subscription("BTCUSDT", "BINANCE", AssetClass.CRYPTO),
        Subscription("ETHUSDT", "BINANCE", AssetClass.CRYPTO),
        Subscription("RELIANCE", "NSE", AssetClass.IN_EQUITY),
        Subscription("GOLD", "MCX", AssetClass.COMMODITY),
        Subscription("AAPL", "NASDAQ", AssetClass.US_EQUITY),
    ]
    try:
        asyncio.run(StreamSupervisor(demo).run_forever())
    except KeyboardInterrupt:
        pass
