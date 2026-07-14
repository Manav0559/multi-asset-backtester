"""Market-data read routes for the dashboard: list assets, fetch recent OHLCV
bars for charting, server-side indicator overlays, plus the live-market
surfaces (open/closed status, last-known tick/depth snapshot, and a
last-session volume-at-price profile that stands in for equity 'depth')."""
import json
import math

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.session import get_db
from app.indicators import IndicatorError, IndicatorService
from app.models import Asset, OhlcvBar, User
from app.models.enums import AssetClass, Timeframe
from app.services.events import _client as _redis
from app.services.market_hours import market_status
from app.streaming.bus import depth_snapshot_key, tick_snapshot_key

router = APIRouter(tags=["market"])


def _asset_or_404(db: Session, asset_id: int) -> Asset:
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="asset not found")
    return asset


@router.get("/market/{asset_id}/status")
def market_status_route(asset_id: int, user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    a = _asset_or_404(db, asset_id)
    return {"asset_id": a.id, "symbol": a.symbol, "exchange": a.exchange,
            **market_status(a.exchange, a.asset_class)}


@router.get("/market/{asset_id}/snapshot")
def market_snapshot(asset_id: int, user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    """Last-known tick + depth from the Redis cache (instant value before the
    first WS frame), with a provenance badge. LIVE only for crypto."""
    a = _asset_or_404(db, asset_id)
    r = _redis()
    tick_raw = r.get(tick_snapshot_key(a.exchange, a.symbol))
    depth_raw = r.get(depth_snapshot_key(a.exchange, a.symbol))
    status = market_status(a.exchange, a.asset_class)
    live = a.asset_class == AssetClass.CRYPTO
    return {
        "asset_id": a.id, "symbol": a.symbol, "exchange": a.exchange,
        "tick": json.loads(tick_raw) if tick_raw else None,
        "depth": json.loads(depth_raw) if depth_raw else None,
        "provenance": "live" if live else status["provenance"],
        "channels": {"tick": f"tick:{a.exchange}:{a.symbol}",
                     "depth": f"depth:{a.exchange}:{a.symbol}"},
        "status": status,
    }


@router.get("/market/{asset_id}/volume-profile")
def volume_profile(asset_id: int, buckets: int = Query(20, ge=5, le=50),
                   user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Last-session volume-at-price, the honest stand-in for equity 'depth'.
    Derived from real stored bars — badged LAST SESSION, never presented as a
    live book."""
    a = _asset_or_404(db, asset_id)
    # Prefer the finest intraday timeframe we have; fall back to daily.
    for tf in (Timeframe.M1, Timeframe.M15, Timeframe.H1, Timeframe.D1):
        rows = db.execute(
            select(OhlcvBar.time, OhlcvBar.close, OhlcvBar.high, OhlcvBar.low,
                   OhlcvBar.volume)
            .where(OhlcvBar.asset_id == asset_id, OhlcvBar.timeframe == tf)
            .order_by(OhlcvBar.time.desc()).limit(400)
        ).all()
        if len(rows) >= 10:
            break
    if not rows:
        raise HTTPException(status_code=404, detail="no bars for volume profile")

    session_date = rows[0].time.date().isoformat()
    lo = float(min(r.low for r in rows))
    hi = float(max(r.high for r in rows))
    span = (hi - lo) or 1.0
    levels = [0.0] * buckets
    for r in rows:
        idx = min(int((float(r.close) - lo) / span * buckets), buckets - 1)
        levels[idx] += float(r.volume)
    step = span / buckets
    profile = [{"price": round(lo + (i + 0.5) * step, 6), "volume": round(v, 4)}
               for i, v in enumerate(levels) if v > 0]
    profile.sort(key=lambda x: x["price"], reverse=True)
    return {"asset_id": a.id, "symbol": a.symbol, "session_date": session_date,
            "provenance": "last_session", "levels": profile}


@router.get("/assets")
def list_assets(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(Asset).where(Asset.is_active).order_by(Asset.symbol)).all()
    return [{"id": a.id, "symbol": a.symbol, "exchange": a.exchange,
             "asset_class": a.asset_class.value, "currency": a.currency} for a in rows]


@router.get("/assets/timeframes")
def asset_timeframes(ids: str, user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)) -> dict:
    """Which timeframes actually have data, per asset — the UI only offers
    those (most equities are 1d-only; intraday history exists for crypto and
    a megacap core). One grouped, chunk-pruned count; capped id list."""
    try:
        asset_ids = [int(x) for x in ids.split(",") if x.strip()][:60]
    except ValueError:
        raise HTTPException(status_code=422, detail="ids must be ints")
    if not asset_ids:
        return {}
    # Counting over compressed chunks costs ~250ms/asset — cache per asset
    # (bars append slowly; staleness of an hour is invisible in a picker).
    r = _redis()
    out: dict[int, list[dict]] = {}
    missing = []
    for a in asset_ids:
        cached = r.get(f"tfavail:{a}")
        if cached is not None:
            out[a] = json.loads(cached)
        else:
            missing.append(a)
    if not missing:
        return out
    asset_ids = missing
    rows = db.execute(
        select(OhlcvBar.asset_id, OhlcvBar.timeframe, func.count())
        .where(OhlcvBar.asset_id.in_(asset_ids))
        .group_by(OhlcvBar.asset_id, OhlcvBar.timeframe)
    ).all()
    fresh: dict[int, list[dict]] = {a: [] for a in asset_ids}
    order = {"1m": 0, "5m": 1, "15m": 2, "1h": 3, "1d": 4}
    for aid, tf, n in rows:
        if n >= 30:  # fewer bars than any indicator warm-up is not chartable
            fresh[aid].append({"timeframe": tf.value, "bars": n})
    for a, tfs in fresh.items():
        tfs.sort(key=lambda x: order.get(x["timeframe"], 9))
        r.set(f"tfavail:{a}", json.dumps(tfs), ex=3600)
        out[a] = tfs
    return out


def _load_bar_frame(db: Session, asset_id: int, tf: Timeframe, limit: int):
    rows = db.execute(
        select(OhlcvBar.time, OhlcvBar.open, OhlcvBar.high, OhlcvBar.low,
               OhlcvBar.close, OhlcvBar.volume)
        .where(OhlcvBar.asset_id == asset_id, OhlcvBar.timeframe == tf)
        .order_by(OhlcvBar.time.desc()).limit(limit)
    ).all()
    return list(reversed(rows))  # chronological for charting


@router.get("/assets/{asset_id}/bars")
def get_bars(asset_id: int, timeframe: str = "1d", limit: int = Query(300, le=2000),
             user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = _load_bar_frame(db, asset_id, Timeframe(timeframe), limit)
    return [{"time": r.time.isoformat(), "open": float(r.open), "high": float(r.high),
             "low": float(r.low), "close": float(r.close), "volume": float(r.volume)}
            for r in rows]


@router.get("/indicators")
def indicator_catalog(user: User = Depends(get_current_user)):
    """All supported indicators (150+) with categories and tunable params —
    drives the chart's indicator picker."""
    return IndicatorService.catalog()


@router.get("/assets/{asset_id}/indicators")
def get_indicators(asset_id: int,
                   spec: str = Query(..., description="e.g. rsi:length=14;macd;bbands:length=20"),
                   timeframe: str = "1d", limit: int = Query(300, le=2000),
                   user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Indicator series aligned to the same bars `GET /assets/{id}/bars`
    returns, so the frontend overlays them 1:1 on the chart."""
    rows = _load_bar_frame(db, asset_id, Timeframe(timeframe), limit)
    if not rows:
        raise HTTPException(status_code=404, detail="no bars for this asset/timeframe")

    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
    df = df.set_index(pd.DatetimeIndex(df["time"])).drop(columns=["time"]).astype(float)

    try:
        specs = IndicatorService.parse_spec(spec)
        computed = IndicatorService.compute(df, specs)
    except IndicatorError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    def _clean(v):
        if v is None or (isinstance(v, float) and not math.isfinite(v)):
            return None
        return round(float(v), 8) if isinstance(v, float) else v

    return {
        "time": [t.isoformat() for t in df.index],
        "series": {str(col): [_clean(v) for v in computed[col].tolist()]
                   for col in computed.columns},
    }
