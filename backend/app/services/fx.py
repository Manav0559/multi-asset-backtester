"""FX conversion for the multi-currency ledger.

Convention: a pair `USDXXX` stores XXX units per 1 USD (USDINR ≈ 83 means
83 rupees to the dollar), so:

    usd_amount = ccy_amount / rate(USD<ccy>)

Portfolio cash is USD (the portfolio's base_currency). Assets quote in their
venue currency (NSE → INR). Every non-USD fill converts its notional through
the latest stored rate; if no rate has EVER been fetched, the trade is
REJECTED — a wrong-unit ledger entry is worse than a rejected order, and the
CHECK constraint can't catch a unit error, only a sign error.

Rates refresh hourly via a beat task from yfinance ("USDINR=X" style
tickers). Vendor-delayed FX (~minutes) is fine for a paper venue; the rate
used is recorded on the ledger note for audit.
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


def pair_for(currency: str) -> str:
    return f"USD{currency.upper()}"


def usd_rate(db: Session, currency: str) -> Decimal | None:
    """Units of `currency` per 1 USD, or None if never fetched."""
    if currency.upper() == "USD":
        return Decimal("1")
    row = db.get(FxRate, pair_for(currency))
    return Decimal(row.rate) if row else None


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
