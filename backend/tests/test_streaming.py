"""Stream adapter layer tests.

Covers the parts that carry real correctness risk:
  * canonical envelope: Decimal precision + JSON round-trip
  * Binance kline normalization from a real message shape
  * yfinance Yahoo-symbol mapping (the vendor-specific quirk)
  * persistence: closed bars land in ohlcv_bars, forming bars don't,
    and re-delivery is idempotent (ON CONFLICT upsert)

Adapter run-loops (live sockets) are not exercised here — they're I/O to
third-party servers; we test the normalization + persistence logic that we
actually own.
"""
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from app.db.session import SessionLocal
from app.models import Asset, OhlcvBar
from app.models.enums import AssetClass, Timeframe
from app.streaming.adapters.yfinance_poll import to_yahoo_symbol
from app.streaming.base import Subscription
from app.streaming.envelope import make_bar, make_tick
from app.streaming.persistence import BarPersister


# ----------------------------------------------------------------- envelope --
def test_tick_decimal_precision_and_json_roundtrip():
    tick = make_tick("BTCUSDT", "BINANCE", AssetClass.CRYPTO,
                     price="50123.12345678", volume="0.001", ts=datetime.now(timezone.utc))
    assert tick.price == Decimal("50123.12345678")  # no float error
    payload = json.loads(tick.to_json())
    assert payload["price"] == "50123.12345678"     # lossless as string
    assert payload["asset_class"] == "crypto"


def test_bar_forming_flag_serialized():
    bar = make_bar("AAPL", "NASDAQ", AssetClass.US_EQUITY, Timeframe.M1,
                   ts=datetime.now(timezone.utc), o=1, h=2, l=1, c=1.5, volume=100,
                   is_closed=False)
    assert json.loads(bar.to_json())["is_closed"] is False


# ------------------------------------------------------- binance normalization --
def test_binance_kline_normalization():
    # Real Binance combined-stream kline message shape.
    raw = json.dumps({
        "stream": "btcusdt@kline_1m",
        "data": {
            "e": "kline", "s": "BTCUSDT",
            "k": {
                "t": 1700000000000, "s": "BTCUSDT", "i": "1m",
                "o": "50000.00", "h": "50100.00", "l": "49900.00", "c": "50050.00",
                "v": "12.5", "n": 340, "x": True,
            },
        },
    })
    from app.streaming.adapters.binance import BinanceAdapter

    captured = []

    class _FakeBus:
        async def publish_bar(self, bar):
            captured.append(bar)

    sub = Subscription("BTCUSDT", "BINANCE", AssetClass.CRYPTO, Timeframe.M1)
    adapter = BinanceAdapter([sub], _FakeBus())  # bus type-duck is fine here

    import asyncio
    asyncio.run(adapter._handle(raw))

    assert len(captured) == 1
    bar = captured[0]
    assert bar.symbol == "BTCUSDT"
    assert bar.close == Decimal("50050.00")
    assert bar.trade_count == 340
    assert bar.is_closed is True
    assert bar.ts == datetime.fromtimestamp(1700000000, tz=timezone.utc)


# ------------------------------------------------------- yfinance symbol map --
@pytest.mark.parametrize("symbol,cls,expected", [
    ("RELIANCE", AssetClass.IN_EQUITY, "RELIANCE.NS"),
    ("^NSEI", AssetClass.IN_INDEX, "^NSEI"),
    ("GOLD", AssetClass.COMMODITY, "GC=F"),
    ("CRUDE", AssetClass.COMMODITY, "CL=F"),
])
def test_yahoo_symbol_mapping(symbol, cls, expected):
    assert to_yahoo_symbol(Subscription(symbol, "X", cls)) == expected


# --------------------------------------------------------------- persistence --
@pytest.fixture()
def crypto_asset():
    """A throwaway asset row for persistence tests; cleaned up after."""
    with SessionLocal() as db:
        sym = f"TST{uuid.uuid4().hex[:6].upper()}"
        asset = Asset(symbol=sym, exchange="BINANCE", asset_class=AssetClass.CRYPTO)
        db.add(asset)
        db.commit()
        db.refresh(asset)
        aid, asym = asset.id, asset.symbol
    yield aid, asym
    with SessionLocal() as db:
        db.execute(delete(OhlcvBar).where(OhlcvBar.asset_id == aid))
        db.execute(delete(Asset).where(Asset.id == aid))
        db.commit()


def _bar_payload(symbol, ts, close, is_closed=True):
    return json.loads(make_bar(symbol, "BINANCE", AssetClass.CRYPTO, Timeframe.M1,
                               ts=ts, o=close, h=close, l=close, c=close, volume=1,
                               is_closed=is_closed).to_json())


def test_persister_writes_closed_bar_and_is_idempotent(crypto_asset):
    aid, asym = crypto_asset
    persister = BarPersister(bus=None)  # _write() doesn't touch the bus
    ts = datetime(2025, 1, 1, tzinfo=timezone.utc)

    persister._write(_bar_payload(asym, ts, "100.00"))
    # re-deliver same PK with a new close -> upsert, not duplicate/error
    persister._write(_bar_payload(asym, ts, "105.00"))

    with SessionLocal() as db:
        rows = db.scalars(select(OhlcvBar).where(OhlcvBar.asset_id == aid)).all()
    assert len(rows) == 1
    assert rows[0].close == Decimal("105.00")


def test_persister_ignores_forming_bar(crypto_asset):
    aid, asym = crypto_asset
    persister = BarPersister(bus=None)
    ts = datetime(2025, 2, 1, tzinfo=timezone.utc)
    persister._write(_bar_payload(asym, ts, "100.00", is_closed=False))
    with SessionLocal() as db:
        rows = db.scalars(select(OhlcvBar).where(OhlcvBar.asset_id == aid)).all()
    assert rows == []
