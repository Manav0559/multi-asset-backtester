"""IndicatorService + market indicator API tests.

The invariants that matter: the catalog is big (100+ names — the
TradingView-equivalent claim), params are validated against real signatures
(no kwargs smuggling), output is aligned to the input index (ichimoku's
forward span must be dropped — lookahead), and the HTTP layer maps bad specs
to 422 with the offending name in the message.
"""
import uuid
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import delete

from app.db.session import SessionLocal
from app.indicators import IndicatorError, IndicatorService, IndicatorSpec
from app.models import Asset, OhlcvBar
from app.models.enums import AssetClass, Timeframe


def _ohlcv(n: int = 120) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    rng = np.random.default_rng(7)
    close = pd.Series(100 + np.cumsum(rng.normal(0, 1, n)), index=idx)
    return pd.DataFrame({
        "open": close.shift(1).fillna(close.iloc[0]),
        "high": close * 1.01, "low": close * 0.99, "close": close,
        "volume": rng.uniform(1e5, 1e6, n),
    }, index=idx)


# ------------------------------------------------------------- service --
def test_catalog_is_tradingview_scale():
    catalog = IndicatorService.catalog()
    assert len(catalog) >= 100
    by_name = {c["name"]: c for c in catalog}
    for expected in ("rsi", "macd", "bbands", "supertrend", "vwap", "ichimoku"):
        assert expected in by_name
    assert any(p["name"] == "length" for p in by_name["rsi"]["params"])


def test_compute_multi_output_aligned():
    df = _ohlcv()
    out = IndicatorService.compute(df, [
        IndicatorSpec("rsi", {"length": 14}),
        IndicatorSpec("macd", {}),
        IndicatorSpec("bbands", {"length": 20}),
    ])
    assert out.index.equals(df.index)          # aligned, no forward-dated rows
    assert any(c.startswith("RSI") for c in out.columns)
    assert sum(c.startswith("MACD") for c in out.columns) == 3   # line/hist/signal
    assert out.filter(like="RSI").iloc[-1].notna().all()


def test_ichimoku_forward_span_dropped():
    df = _ohlcv()
    out = IndicatorService.compute(df, [IndicatorSpec("ichimoku", {})])
    # the forward senkou projection extends past the last bar — it must NOT
    assert out.index.max() == df.index.max()
    assert len(out) == len(df)


def test_unknown_indicator_and_bad_params_rejected():
    df = _ohlcv()
    with pytest.raises(IndicatorError, match="unknown indicator"):
        IndicatorService.compute(df, [IndicatorSpec("__import__", {})])
    with pytest.raises(IndicatorError, match="unknown param"):
        IndicatorService.compute(df, [IndicatorSpec("rsi", {"evil": 1})])


def test_parse_spec_roundtrip():
    specs = IndicatorService.parse_spec("rsi:length=14;macd;bbands:length=20,std=2.5")
    assert specs[0] == IndicatorSpec("rsi", {"length": 14})
    assert specs[1] == IndicatorSpec("macd", {})
    assert specs[2].params == {"length": 20, "std": 2.5}


# ----------------------------------------------------------------- api --
@pytest.fixture()
def chart_asset(client):
    """An asset with 90 daily bars + an authed user; sweeps itself."""
    suffix = uuid.uuid4().hex[:8]
    email, pw = f"ind_{suffix}@example.com", "s3cret-pass!"
    client.post("/auth/register", json={"email": email, "username": f"ind_{suffix}", "password": pw})
    token = client.post("/auth/login", json={"email": email, "password": pw}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    with SessionLocal() as db:
        asset = Asset(symbol=f"IND{suffix[:5].upper()}", exchange="TEST",
                      asset_class=AssetClass.CRYPTO)
        db.add(asset); db.commit(); db.refresh(asset)
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        for i in range(90):
            px = 100 + i * 0.5
            db.add(OhlcvBar(asset_id=asset.id, timeframe=Timeframe.D1,
                            time=base + timedelta(days=i),
                            open=px, high=px * 1.01, low=px * 0.99, close=px, volume=1000))
        db.commit()
        asset_id = asset.id
    yield {"asset_id": asset_id, "headers": headers}
    with SessionLocal() as db:
        db.execute(delete(OhlcvBar).where(OhlcvBar.asset_id == asset_id))
        db.execute(delete(Asset).where(Asset.id == asset_id))
        db.commit()


def test_indicator_api_catalog_and_series(client, chart_asset):
    r = client.get("/indicators", headers=chart_asset["headers"])
    assert r.status_code == 200 and len(r.json()) >= 100

    r = client.get(f"/assets/{chart_asset['asset_id']}/indicators",
                   params={"spec": "rsi:length=14;bbands:length=20", "timeframe": "1d"},
                   headers=chart_asset["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["time"]) == 90
    rsi_col = next(c for c in body["series"] if c.startswith("RSI"))
    series = body["series"][rsi_col]
    assert len(series) == 90
    assert series[0] is None                # warm-up is null, not NaN-poisoned
    assert isinstance(series[-1], float)    # steady climb -> RSI defined & high
    assert series[-1] > 50


def test_indicator_api_bad_spec_422(client, chart_asset):
    r = client.get(f"/assets/{chart_asset['asset_id']}/indicators",
                   params={"spec": "totally_fake"}, headers=chart_asset["headers"])
    assert r.status_code == 422
    assert "totally_fake" in r.json()["detail"]
