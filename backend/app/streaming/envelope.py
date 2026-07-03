"""The canonical market-data contract.

Every adapter (Binance, yfinance, Alpaca, ...) normalizes its
source-specific payload into these two frozen dataclasses BEFORE anything
downstream sees it. The WS hub, the persistence writer, and the frontend
therefore never learn about source-specific schemas — swapping or adding a
data vendor touches exactly one adapter file.

Two message shapes:
  * Tick  — a single trade/price update (sub-second, unaggregated).
  * Bar   — a completed OHLCV candle for a (symbol, timeframe).

Prices/volumes are Decimal to preserve precision on the way into the
Numeric columns of ohlcv_bars (floats would lose cents/satoshis).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal

from app.models.enums import AssetClass, Timeframe


def _dec(v) -> Decimal:
    # str() first so we never inherit binary float error (Decimal(0.1) is ugly).
    return v if isinstance(v, Decimal) else Decimal(str(v))


@dataclass(frozen=True, slots=True)
class Tick:
    symbol: str            # canonical, e.g. "BTCUSDT", "RELIANCE", "AAPL"
    exchange: str          # "BINANCE", "NSE", "NASDAQ", ...
    asset_class: AssetClass
    price: Decimal
    volume: Decimal        # trade size (0 if source doesn't provide)
    ts: datetime           # tz-aware UTC event time

    def to_json(self) -> str:
        return json.dumps(_serialize(self))


@dataclass(frozen=True, slots=True)
class Bar:
    symbol: str
    exchange: str
    asset_class: AssetClass
    timeframe: Timeframe
    ts: datetime           # tz-aware UTC, the bar's OPEN time
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    trade_count: int | None = None
    vwap: Decimal | None = None
    is_closed: bool = True  # False => still-forming (live) bar; don't persist yet

    def to_json(self) -> str:
        return json.dumps(_serialize(self))


def make_tick(symbol, exchange, asset_class, price, volume, ts) -> Tick:
    return Tick(
        symbol=symbol,
        exchange=exchange,
        asset_class=AssetClass(asset_class) if not isinstance(asset_class, AssetClass) else asset_class,
        price=_dec(price),
        volume=_dec(volume),
        ts=_ensure_utc(ts),
    )


def make_bar(symbol, exchange, asset_class, timeframe, ts, o, h, l, c, volume,
             trade_count=None, vwap=None, is_closed=True) -> Bar:
    return Bar(
        symbol=symbol,
        exchange=exchange,
        asset_class=AssetClass(asset_class) if not isinstance(asset_class, AssetClass) else asset_class,
        timeframe=Timeframe(timeframe) if not isinstance(timeframe, Timeframe) else timeframe,
        ts=_ensure_utc(ts),
        open=_dec(o), high=_dec(h), low=_dec(l), close=_dec(c),
        volume=_dec(volume),
        trade_count=trade_count,
        vwap=_dec(vwap) if vwap is not None else None,
        is_closed=is_closed,
    )


def _ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _serialize(obj) -> dict:
    """JSON-safe dict: Decimals -> str (lossless), datetimes -> ISO,
    enums -> their .value."""
    out = {}
    for k, v in asdict(obj).items():
        if isinstance(v, Decimal):
            out[k] = str(v)
        elif isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, (AssetClass, Timeframe)):
            out[k] = v.value
        else:
            out[k] = v
    return out
