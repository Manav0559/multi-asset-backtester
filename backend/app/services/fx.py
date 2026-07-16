"""FX conversion for the multi-currency ledger.

Convention: a pair `USDXXX` stores XXX units per 1 USD (USDINR ≈ 83 means
83 rupees to the dollar), so:

    usd_amount = ccy_amount / rate(USD<ccy>)

Portfolio cash is USD (the portfolio's base_currency). Assets quote in their
venue currency (NSE → INR). Every non-USD fill converts its notional through
the latest stored rate.

Rates refresh hourly via the scheduler from yfinance ("USDINR=X" style
tickers), but that first tick is an hour out — so `ensure_usd_rate` fetches a
missing rate ON DEMAND at execution time, and falls back to a sane constant if
the fetch is unavailable, so an international trade never blocks on a cold feed.
Vendor-delayed FX (~minutes) is fine for a paper venue; the rate used is
recorded on the ledger note for audit.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import FxRate

logger = logging.getLogger("services.fx")

# Currencies the platform knows how to convert to the USD ledger base.
SUPPORTED = ("INR",)

# Last-resort spot rates (units of ccy per 1 USD) used only when the live fetch
# is unavailable, so a foreign-venue trade never blocks on a cold FX feed. A
# labeled approximation beats a rejected order for a paper venue.
FALLBACK_RATES: dict[str, Decimal] = {"INR": Decimal("86")}


def pair_for(currency: str) -> str:
    return f"USD{currency.upper()}"


def usd_rate(db: Session, currency: str) -> Decimal | None:
    """Units of `currency` per 1 USD from the store, or None if never fetched."""
    if currency.upper() == "USD":
        return Decimal("1")
    row = db.get(FxRate, pair_for(currency))
    return Decimal(row.rate) if row else None


def _fetch_spot(pair: str) -> Decimal | None:
    """Live spot for one USDccy pair from yfinance, or None if unavailable."""
    try:
        import yfinance as yf
        px = yf.Ticker(f"{pair}=X").fast_info.last_price
        if px and px > 0:
            return Decimal(str(px))
    except Exception as exc:  # noqa: BLE001 — network/vendor hiccup -> fall back
        logger.warning("on-demand fx fetch failed for %s: %s", pair, exc)
    return None


def ensure_usd_rate(db: Session, currency: str) -> Decimal | None:
    """Rate for the ledger, resolved on demand. USD is 1. For a supported
    currency: use the stored rate; if none, fetch spot now and persist it; if
    the fetch fails, use the fallback constant. Returns None only for a currency
    the platform does not support at all (so execution can reject honestly)."""
    cur = currency.upper()
    if cur == "USD":
        return Decimal("1")
    stored = usd_rate(db, cur)
    if stored is not None and stored > 0:
        return stored
    if cur not in SUPPORTED and cur not in FALLBACK_RATES:
        return None
    pair = pair_for(cur)
    rate = _fetch_spot(pair) or FALLBACK_RATES.get(cur)
    if rate is None:
        return None
    # Persist so the next trade is a cheap read and the ledger note is stable.
    row = db.get(FxRate, pair)
    if row is None:
        db.add(FxRate(pair=pair, rate=rate))
    else:
        row.rate = rate
    db.flush()
    return rate


def to_usd(db: Session, amount: Decimal, currency: str) -> Decimal | None:
    rate = usd_rate(db, currency)
    if rate is None or rate <= 0:
        return None
    return amount / rate


def refresh_fx_rates() -> dict:
    """Beat task body: pull the latest spot for every supported pair from
    yfinance and upsert. Failures leave the previous rate in place (stale
    beats absent; `updated_at` records honesty)."""
    import yfinance as yf

    from app.db.session import SessionLocal

    out: dict[str, str] = {}
    with SessionLocal() as db:
        for ccy in SUPPORTED:
            pair = pair_for(ccy)
            try:
                px = yf.Ticker(f"{pair}=X").fast_info.last_price
                if not px or px <= 0:
                    raise ValueError(f"bad price {px!r}")
                row = db.get(FxRate, pair)
                if row is None:
                    db.add(FxRate(pair=pair, rate=Decimal(str(px))))
                else:
                    row.rate = Decimal(str(px))
                out[pair] = f"{px:.4f}"
            except Exception as exc:  # noqa: BLE001 — keep the previous rate
                logger.warning("fx refresh failed for %s: %s", pair, exc)
                out[pair] = f"FAIL {type(exc).__name__}"
        db.commit()
    return out
