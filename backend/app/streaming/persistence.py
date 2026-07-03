"""Bar persistence writer.

Subscribes to `bar:*` on the tick bus and upserts CLOSED bars into the
ohlcv_bars hypertable, resolving canonical (symbol, exchange) to asset_id.
Still-forming bars (is_closed=False) are ignored — only finalized candles
are stored, so history is never polluted by partial data.

Uses ON CONFLICT on the (asset_id, timeframe, time) PK so re-delivered or
backfilled bars update in place instead of erroring (idempotent ingest).
Unknown symbols are skipped with a warning rather than crashing the writer.
"""
from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.session import SessionLocal
from app.models import Asset, OhlcvBar
from app.streaming.bus import TickBus

logger = logging.getLogger("streaming.persistence")


class BarPersister:
    def __init__(self, bus: TickBus):
        self.bus = bus
        self._asset_cache: dict[tuple[str, str], int] = {}

    def _resolve_asset_id(self, symbol: str, exchange: str) -> int | None:
        key = (symbol, exchange)
        if key in self._asset_cache:
            return self._asset_cache[key]
        with SessionLocal() as db:
            asset_id = db.scalar(
                select(Asset.id).where(Asset.symbol == symbol, Asset.exchange == exchange)
            )
        if asset_id is not None:
            self._asset_cache[key] = asset_id
        return asset_id

    def _write(self, payload: dict) -> None:
        if not payload.get("is_closed", True):
            return  # never persist a still-forming candle
        asset_id = self._resolve_asset_id(payload["symbol"], payload["exchange"])
        if asset_id is None:
            logger.warning("no asset row for %s/%s; bar dropped",
                           payload["exchange"], payload["symbol"])
            return

        row = {
            "asset_id": asset_id,
            "timeframe": payload["timeframe"],
            "time": payload["ts"],
            "open": payload["open"], "high": payload["high"],
            "low": payload["low"], "close": payload["close"],
            "volume": payload["volume"],
            "trade_count": payload.get("trade_count"),
            "vwap": payload.get("vwap"),
        }
        stmt = pg_insert(OhlcvBar).values(**row)
        stmt = stmt.on_conflict_do_update(
            index_elements=["asset_id", "timeframe", "time"],
            set_={k: row[k] for k in ("open", "high", "low", "close",
                                      "volume", "trade_count", "vwap")},
        )
        with SessionLocal() as db:
            db.execute(stmt)
            db.commit()

    async def run(self) -> None:
        """Subscribe to all bar channels and persist closed bars forever."""
        pubsub = self.bus.redis.pubsub()
        await pubsub.psubscribe("bar:*")
        logger.info("bar persister subscribed to bar:*")
        try:
            async for message in pubsub.listen():
                if message["type"] != "pmessage":
                    continue
                payload = json.loads(message["data"])
                # DB work is blocking; hand to a thread so we keep draining.
                import asyncio
                await asyncio.to_thread(self._write, payload)
        finally:
            await pubsub.aclose()
