"""On-demand "live-ish" pricing for portfolio and competition views.

The free-tier architecture has no ticker daemon, so portfolio and challenge
reads refresh the latest price for ONLY the assets they need, on demand, then
compute totals from the fresh marks. A short in-process TTL cache bounds vendor
calls: however many clients poll (every ~30s), each symbol is fetched at most
once per `_TTL_S`, and the four endpoints a single page load hits never stampede
the feed. Fresh prices are upserted onto the current-session daily bar (all D1
bars are canonical 00:00 UTC), so the existing valuation SQL — which marks at
the latest stored close — picks them up unchanged, and today's chart candle
updates too. Still DELAYED-badged: this is an on-demand poll, not a live feed.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import Asset, OhlcvBar
from app.models.enums import AssetClass, Timeframe

logger = logging.getLogger("services.live_pricing")

# Kept just under the client poll cadence so a steady 30s poll gets fresh marks
# while bursts (the 4 reads per page load, or several viewers) share one fetch.
_TTL_S = 25.0
_cache: dict[int, tuple[Decimal, float]] = {}   # asset_id -> (price, monotonic ts)
_lock = threading.Lock()


def _cached_fresh(asset_id: int) -> Decimal | None:
    hit = _cache.get(asset_id)
    if hit and (time.monotonic() - hit[1]) < _TTL_S:
        return hit[0]
    return None


def _fetch_one(symbol: str, exchange: str, asset_class: AssetClass) -> Decimal | None:
    """Latest price from the venue — Binance REST for crypto, yfinance otherwise.
    Returns None on any hiccup so the caller keeps the previous stored close."""
    try:
        if asset_class == AssetClass.CRYPTO:
            import httpx
            with httpx.Client(timeout=4.0) as c:
                r = c.get("https://api.binance.com/api/v3/ticker/price",
                          params={"symbol": symbol})
            if r.status_code != 200:
                return None
            px = r.json().get("price")
            return Decimal(str(px)) if px else None
        import yfinance as yf
        from app.streaming.adapters.yfinance_poll import to_yahoo_symbol
        from app.streaming.base import Subscription
        yf_sym = to_yahoo_symbol(Subscription(symbol, exchange, asset_class))
        fi = yf.Ticker(yf_sym).fast_info
        px = fi.get("lastPrice") or fi.get("last_price")
        return Decimal(str(px)) if px else None
    except Exception as exc:  # noqa: BLE001 — vendor hiccup; keep the stored close
        logger.warning("live price fetch failed for %s/%s: %s", exchange, symbol, exc)
        return None


def _session_time() -> datetime:
    """Canonical daily-bar timestamp for the current UTC session (00:00)."""
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def sync_prices(db: Session, asset_ids: set[int]) -> int:
    """Refresh latest prices for the given assets (deduped), skipping any that
    are cache-fresh, and upsert each onto today's daily bar so valuations read
    the new close. Best-effort: a failed fetch keeps the previous close, and any
    DB error rolls back and returns 0 rather than breaking the read it fronts.
    Returns the count freshly fetched + stored."""
    ids = {a for a in asset_ids if a and _cached_fresh(a) is None}
    if not ids:
        return 0
    try:
        rows = db.execute(
            select(Asset.id, Asset.symbol, Asset.exchange, Asset.asset_class)
            .where(Asset.id.in_(ids))
        ).all()
        if not rows:
            return 0

        def work(row):
            aid, symbol, exchange, klass = row
            return aid, _fetch_one(symbol, exchange, klass)

        fetched: list[tuple[int, Decimal]] = []
        with ThreadPoolExecutor(max_workers=min(8, len(rows))) as pool:
            for aid, px in pool.map(work, rows):
                if px and px > 0:
                    fetched.append((aid, px))
                    with _lock:
                        _cache[aid] = (px, time.monotonic())
        if not fetched:
            return 0

        session_t = _session_time()
        for aid, px in fetched:
            ins = pg_insert(OhlcvBar).values(
                asset_id=aid, timeframe=Timeframe.D1, time=session_t,
                open=px, high=px, low=px, close=px, volume=0,
            )
            db.execute(ins.on_conflict_do_update(
                index_elements=["asset_id", "timeframe", "time"],
                set_={"close": px,
                      "high": func.greatest(OhlcvBar.high, ins.excluded.high),
                      "low": func.least(OhlcvBar.low, ins.excluded.low)},
            ))
        db.commit()
        return len(fetched)
    except Exception as exc:  # noqa: BLE001 — never let pricing break the read
        logger.warning("sync_prices failed: %s", exc)
        db.rollback()
        return 0
