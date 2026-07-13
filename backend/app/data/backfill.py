"""Historical OHLCV backfill → ohlcv_bars.

Loads *actual* historical bars so the backtest engine runs on real prices
(not synthetic data). Two sources in Phase 1:

  * yfinance  — US equities, NSE equities/indexes, commodities. Daily and
    intraday history. Reuses the exact Yahoo-symbol mapping from the live
    yfinance adapter so a symbol means the same thing in backfill and live.
  * Binance REST klines — crypto history (up to 1000 bars/request, paged).

Idempotent: upserts on the (asset_id, timeframe, time) PK, so re-running a
backfill over an overlapping window updates in place instead of duplicating.
Auto-creates the assets row if missing.

This is an operator/ingestion tool (run via `python -m app.data.backfill`
or called from a scheduled job), separate from the live streaming path.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models import Asset, OhlcvBar
from app.models.enums import AssetClass, Timeframe
from app.streaming.adapters.yfinance_poll import to_yahoo_symbol
from app.streaming.base import Subscription

logger = logging.getLogger("data.backfill")

# our timeframe -> yfinance interval / binance interval
_YF_INTERVAL = {Timeframe.M1: "1m", Timeframe.M5: "5m", Timeframe.M15: "15m",
                Timeframe.H1: "60m", Timeframe.D1: "1d"}
_BINANCE_INTERVAL = {Timeframe.M1: "1m", Timeframe.M5: "5m", Timeframe.M15: "15m",
                     Timeframe.H1: "1h", Timeframe.D1: "1d"}


def ensure_asset(db: Session, symbol: str, exchange: str,
                 asset_class: AssetClass) -> int:
    asset_id = db.scalar(
        select(Asset.id).where(Asset.symbol == symbol, Asset.exchange == exchange)
    )
    if asset_id is None:
        # Quote currency follows the venue: NSE/BSE quote in rupees. Getting
        # this wrong isn't cosmetic — the ledger converts fills through it.
        currency = "INR" if exchange.upper() in ("NSE", "BSE") else "USD"
        asset = Asset(symbol=symbol, exchange=exchange, asset_class=asset_class,
                      currency=currency)
        db.add(asset)
        db.commit()
        db.refresh(asset)
        asset_id = asset.id
    return asset_id


def _upsert_bars(db: Session, asset_id: int, timeframe: Timeframe, rows: list[dict]) -> int:
    if not rows:
        return 0
    for r in rows:
        r["asset_id"] = asset_id
        r["timeframe"] = timeframe
    stmt = pg_insert(OhlcvBar).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["asset_id", "timeframe", "time"],
        set_={c: stmt.excluded[c] for c in ("open", "high", "low", "close", "volume")},
    )
    db.execute(stmt)
    db.commit()
    return len(rows)


def backfill_yfinance(symbol: str, exchange: str, asset_class: AssetClass,
                      timeframe: Timeframe = Timeframe.D1, period: str = "5y") -> int:
    """Backfill from Yahoo. `period` e.g. '5y','1y','60d' (intraday limited
    by Yahoo to ~60d for 1m). Returns rows written."""
    import yfinance as yf

    yf_symbol = to_yahoo_symbol(Subscription(symbol, exchange, asset_class, timeframe))
    df = yf.download(yf_symbol, period=period, interval=_YF_INTERVAL[timeframe],
                     progress=False, auto_adjust=False)
    if df is None or df.empty:
        logger.warning("yfinance returned no data for %s", yf_symbol)
        return 0

    rows = []
    for ts, row in df.iterrows():
        pyts = ts.to_pydatetime()
        if pyts.tzinfo is None:
            pyts = pyts.replace(tzinfo=timezone.utc)
        rows.append({
            "time": pyts.astimezone(timezone.utc),
            "open": _d(row, "Open"), "high": _d(row, "High"),
            "low": _d(row, "Low"), "close": _d(row, "Close"),
            "volume": _d(row, "Volume", default="0"),
        })

    with SessionLocal() as db:
        asset_id = ensure_asset(db, symbol, exchange, asset_class)
        n = _upsert_bars(db, asset_id, timeframe, rows)
    logger.info("yfinance backfill %s: %d bars", yf_symbol, n)
    return n


def backfill_binance(symbol: str, timeframe: Timeframe = Timeframe.D1,
                     limit: int = 1000) -> int:
    """Backfill crypto history from Binance REST klines."""
    import requests

    resp = requests.get(
        "https://api.binance.com/api/v3/klines",
        params={"symbol": symbol.upper(), "interval": _BINANCE_INTERVAL[timeframe],
                "limit": limit},
        timeout=15,
    )
    resp.raise_for_status()
    rows = []
    for k in resp.json():
        rows.append({
            "time": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc),
            "open": Decimal(str(k[1])), "high": Decimal(str(k[2])),
            "low": Decimal(str(k[3])), "close": Decimal(str(k[4])),
            "volume": Decimal(str(k[5])),
        })
    with SessionLocal() as db:
        asset_id = ensure_asset(db, symbol, "BINANCE", AssetClass.CRYPTO)
        n = _upsert_bars(db, asset_id, timeframe, rows)
    logger.info("binance backfill %s: %d bars", symbol, n)
    return n


def _d(row, key, default="0") -> Decimal:
    """Pull a scalar Decimal from a pandas row, tolerating NaN / MultiIndex."""
    val = row[key]
    try:
        # yfinance can return a 1-element Series per cell with group_by columns
        if hasattr(val, "item"):
            val = val.item()
    except (ValueError, AttributeError):
        pass
    if val != val:  # NaN
        return Decimal(default)
    return Decimal(str(val))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    backfill_yfinance("AAPL", "NASDAQ", AssetClass.US_EQUITY, Timeframe.D1, "5y")
    backfill_yfinance("RELIANCE", "NSE", AssetClass.IN_EQUITY, Timeframe.D1, "5y")
