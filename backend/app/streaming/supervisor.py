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


def load_crypto_subscriptions(limit: int = 12) -> list[Subscription]:
    """Stream the crypto universe from the `assets` table (the only LIVE feed).
    Equities go through the beat-driven delayed poll, not this supervisor."""
    from sqlalchemy import select

    from app.db.session import SessionLocal
    from app.models import Asset

    with SessionLocal() as db:
        rows = db.execute(
            select(Asset.symbol, Asset.exchange)
            .where(Asset.asset_class == AssetClass.CRYPTO)
            .order_by(Asset.symbol).limit(limit)
        ).all()
    subs = [Subscription(sym, exch, AssetClass.CRYPTO) for sym, exch in rows]
    if not subs:  # empty DB (fresh volume) — stream the majors so the demo is alive
        subs = [Subscription(s, "BINANCE", AssetClass.CRYPTO)
                for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT")]
    return subs


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    subs = load_crypto_subscriptions()
    logger.info("ticker streaming %d crypto symbols: %s",
                len(subs), ", ".join(s.symbol for s in subs))
    try:
        asyncio.run(StreamSupervisor(subs).run_forever())
    except KeyboardInterrupt:
        pass
