"""Market-data read routes for the dashboard: list assets, fetch recent OHLCV
bars for charting, and compute server-side indicator overlays (IndicatorService
— the same engine the backtester uses, so chart and backtest never disagree)."""
import math

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.session import get_db
from app.indicators import IndicatorError, IndicatorService
from app.models import Asset, OhlcvBar, User
from app.models.enums import Timeframe

router = APIRouter(tags=["market"])


@router.get("/assets")
def list_assets(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(Asset).where(Asset.is_active).order_by(Asset.symbol)).all()
    return [{"id": a.id, "symbol": a.symbol, "exchange": a.exchange,
             "asset_class": a.asset_class.value, "currency": a.currency} for a in rows]


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
