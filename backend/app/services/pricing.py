"""Latest-price resolution for paper-trade fills.

v1 fill model (agreed in design): market orders fill at the latest known
CLOSE from ohlcv_bars for the asset (most recent bar across timeframes).
A Redis last-tick cache can front this later without changing callers.

Returns None when no price is known so the caller can reject the order
cleanly ("no market price available") instead of filling at a bad value.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OhlcvBar


def latest_price(db: Session, asset_id: int) -> Decimal | None:
    return db.scalar(
        select(OhlcvBar.close)
        .where(OhlcvBar.asset_id == asset_id)
        .order_by(OhlcvBar.time.desc())
        .limit(1)
    )
