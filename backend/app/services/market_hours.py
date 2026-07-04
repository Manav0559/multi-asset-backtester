"""Market open/closed state per exchange, backed by exchange_calendars.

Crypto trades 24/7 (always open). Equity exchanges follow their real trading
calendar — used to freeze the live view and show "Market closed — showing last
session" outside hours. Unknown exchanges degrade to `is_open=None` (unknown)
rather than lying either way.
"""
from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache

from app.models.enums import AssetClass

# Our exchange label -> exchange_calendars ISO code.
_CALENDAR = {
    "NASDAQ": "XNAS",
    "NYSE": "XNYS",
    "NSE": "XBOM",     # BSE/NSE share Indian equity hours (09:15–15:30 IST)
    "BSE": "XBOM",
}


@lru_cache(maxsize=8)
def _calendar(code: str):
    import exchange_calendars as xc
    return xc.get_calendar(code)


def market_status(exchange: str, asset_class: AssetClass,
                  at: datetime | None = None) -> dict:
    """{is_open, next_open, next_close, session_label, provenance} for a market."""
    now = at or datetime.now(timezone.utc)

    if asset_class == AssetClass.CRYPTO:
        return {"is_open": True, "next_open": None, "next_close": None,
                "label": "24/7", "provenance": "live"}

    code = _CALENDAR.get(exchange.upper())
    if code is None:
        return {"is_open": None, "next_open": None, "next_close": None,
                "label": "unknown market", "provenance": "delayed"}

    import pandas as pd
    cal = _calendar(code)
    ts = pd.Timestamp(now)
    try:
        is_open = bool(cal.is_open_on_minute(ts))
        next_open = cal.next_open(ts).isoformat() if not is_open else None
        next_close = cal.next_close(ts).isoformat() if is_open else None
    except Exception:  # noqa: BLE001 — outside the calendar's bounds, etc.
        is_open, next_open, next_close = None, None, None

    return {
        "is_open": is_open, "next_open": next_open, "next_close": next_close,
        "label": "open" if is_open else "closed",
        # Equities are always delayed; when closed we show the last session.
        "provenance": "delayed" if is_open else "last_session",
    }
