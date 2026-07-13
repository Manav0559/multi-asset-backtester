"""Backfill the full demo asset universe with real history.

    docker compose --profile app exec backend python scripts/backfill_universe.py

Universe:
  - NASDAQ-100 constituents        — 5y daily (yfinance)
  - NIFTY 50 constituents (NSE)    — 5y daily (yfinance, .NS mapping is automatic)
  - Top-10 cryptos (Binance)       — 1d + 1h + 15m + 1m, 1000 bars each
  - A liquid intraday core (US + IN megacaps) — 1h (2y), 15m (60d), 1m (7d);
    Yahoo hard-limits intraday history, so only a core set gets intraday bars.

Idempotent and resilient: symbols with enough bars are skipped, failures are
logged and skipped (one delisted ticker must not sink the other 159), and a
small sleep between Yahoo calls keeps us under their throttle.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, select

from app.data.backfill import backfill_binance, backfill_yfinance
from app.db.session import SessionLocal
from app.models import Asset, OhlcvBar
from app.models.enums import AssetClass, Timeframe

# Current constituents per the NASDAQ-100 membership list (verified against
# Wikipedia's navbox + per-company infoboxes, 2026-07-13). Notable newer
# members validated by infobox, not memory: HONA (Honeywell Aerospace split),
# SPCX (SpaceX post-IPO), CRWV, NBIS, ALAB, RKLB, SNDK (WDC spin).
NASDAQ_100 = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "GOOG", "AVGO", "TSLA",
    "COST", "NFLX", "ASML", "AMD", "PEP", "ADBE", "LIN", "CSCO", "TMUS",
    "QCOM", "INTU", "AMAT", "TXN", "CMCSA", "ISRG", "AMGN", "HON", "BKNG",
    "PANW", "ADP", "VRTX", "GILD", "SBUX", "MU", "ADI", "LRCX", "INTC",
    "MDLZ", "REGN", "KLAC", "CTAS", "PYPL", "SNPS", "CDNS", "MAR", "CSX",
    "ORLY", "CRWD", "ABNB", "FTNT", "PCAR", "NXPI", "MRVL", "CEG", "DASH",
    "ROP", "WDAY", "MNST", "ADSK", "AEP", "FANG", "PAYX",
    "ODFL", "ROST", "KDP", "FAST", "EA", "GEHC", "BKR",
    "XEL", "EXC", "KHC", "CCEP", "DDOG", "IDXX",
    "TTWO", "DXCM", "WBD",
    "MELI", "PDD", "ARM",
    # 2025-26 additions/newly public:
    "ALNY", "APP", "ALAB", "AXON", "CPRT", "CRWV", "FER", "HONA", "LITE",
    "MCHP", "MSTR", "MPWR", "NBIS", "PLTR", "RKLB", "SNDK", "STX", "SHOP",
    "SPCX", "TER", "TRI", "WMT", "WDC",
]

# Departed members kept in the DB for history (survivorship honesty — the
# universe table is still point-in-time-naive; see README limitation):
NASDAQ_DEPARTED = [
    "CHTR", "TTD", "VRSK", "CTSH", "TEAM", "ZS", "ON", "CSGP", "CDW",
    "BIIB", "GFS", "MDB", "LULU", "SMCI",
]

NIFTY_50 = [
    "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "ITC", "BHARTIARTL",
    "SBIN", "LT", "KOTAKBANK", "HINDUNILVR", "AXISBANK", "BAJFINANCE",
    "MARUTI", "ASIANPAINT", "HCLTECH", "SUNPHARMA", "TITAN", "ULTRACEMCO",
    # TATAMOTORS + LTIM omitted: tickers retired after 2025 corporate actions
    "WIPRO", "ONGC", "NTPC", "TRENT", "ADANIENT", "ADANIPORTS",
    "POWERGRID", "M&M", "TATASTEEL", "COALINDIA", "BAJAJFINSV", "NESTLEIND",
    "GRASIM", "JSWSTEEL", "HINDALCO", "TECHM", "INDUSINDBK", "DRREDDY",
    "CIPLA", "EICHERMOT", "APOLLOHOSP", "DIVISLAB", "BRITANNIA",
    "TATACONSUM", "HEROMOTOCO", "BAJAJ-AUTO", "BPCL", "SHRIRAMFIN", "BEL",
    "SBILIFE", "HDFCLIFE",
]

TOP_CRYPTO = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT",
]

# Intraday history is expensive (Yahoo caps: 1m≈7d, 15m≈60d, 1h≈2y) — only
# the most-charted names get it. Crypto intraday comes free from Binance.
INTRADAY_CORE_US = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AVGO"]
INTRADAY_CORE_IN = ["RELIANCE", "TCS", "HDFCBANK"]

_YF_THROTTLE_S = 0.25


def _bar_count(symbol: str, tf: Timeframe) -> int:
    with SessionLocal() as db:
        return db.scalar(
            select(func.count()).select_from(OhlcvBar)
            .join(Asset, Asset.id == OhlcvBar.asset_id)
            .where(Asset.symbol == symbol, OhlcvBar.timeframe == tf)
        ) or 0


def _yf(symbol: str, exchange: str, asset_class: AssetClass,
        tf: Timeframe, period: str, min_have: int) -> str:
    if _bar_count(symbol, tf) >= min_have:
        return "have"
    try:
        n = backfill_yfinance(symbol, exchange, asset_class, timeframe=tf, period=period)
        time.sleep(_YF_THROTTLE_S)
        return f"{n} bars" if n else "EMPTY"
    except Exception as exc:  # noqa: BLE001 — one bad ticker must not sink the run
        return f"FAIL {type(exc).__name__}"


def main() -> None:
    print(f"== NASDAQ-100: {len(NASDAQ_100)} symbols, 5y daily ==")
    for i, sym in enumerate(NASDAQ_100, 1):
        status = _yf(sym, "NASDAQ", AssetClass.US_EQUITY, Timeframe.D1, "5y", min_have=1000)
        print(f"  [{i:>3}/{len(NASDAQ_100)}] {sym:<6} {status}")

    print(f"== NIFTY 50: {len(NIFTY_50)} symbols, 5y daily ==")
    for i, sym in enumerate(NIFTY_50, 1):
        status = _yf(sym, "NSE", AssetClass.IN_EQUITY, Timeframe.D1, "5y", min_have=1000)
        print(f"  [{i:>3}/{len(NIFTY_50)}] {sym:<12} {status}")

    print(f"== crypto: {len(TOP_CRYPTO)} symbols, 1d/1h/15m/1m ==")
    for sym in TOP_CRYPTO:
        for tf in (Timeframe.D1, Timeframe.H1, Timeframe.M15, Timeframe.M1):
            if _bar_count(sym, tf) >= 900:
                print(f"  {sym:<10} {tf.value:<4} have")
                continue
            try:
                n = backfill_binance(sym, timeframe=tf, limit=1000)
                print(f"  {sym:<10} {tf.value:<4} {n} bars")
            except Exception as exc:  # noqa: BLE001
                print(f"  {sym:<10} {tf.value:<4} FAIL {type(exc).__name__}")

    print("== intraday core (Yahoo caps history hard) ==")
    core = [(s, "NASDAQ", AssetClass.US_EQUITY) for s in INTRADAY_CORE_US] + \
           [(s, "NSE", AssetClass.IN_EQUITY) for s in INTRADAY_CORE_IN]
    for sym, exch, ac in core:
        for tf, period, min_have in ((Timeframe.H1, "2y", 500),
                                     (Timeframe.M15, "60d", 300),
                                     (Timeframe.M1, "7d", 300)):
            status = _yf(sym, exch, ac, tf, period, min_have)
            print(f"  {sym:<12} {tf.value:<4} {status}")

    with SessionLocal() as db:
        assets = db.scalar(select(func.count()).select_from(Asset))
        bars = db.scalar(select(func.count()).select_from(OhlcvBar))
    print(f"\n✔ universe ready: {assets} assets, {bars:,} bars")


if __name__ == "__main__":
    main()
