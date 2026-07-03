"""One-command demo seed — boots a fresh stack into a fully-populated app.

    docker compose --profile app exec backend python scripts/demo_seed.py
    (or ./scripts/demo.sh from the repo root, which wraps exactly that)

What it does, idempotently (safe to re-run):
  1. Backfills REAL history: AAPL + MSFT (5y daily via yfinance) and
     BTCUSDT (1000 daily bars via Binance REST).
  2. Two demo users -> two PUBLIC portfolios -> real trades through the
     order API (so ledgers, positions, and the leaderboard are live).
  3. One backtest of each kind through the real API + Celery worker:
     classic (sma_crossover), portfolio (cross_sectional_momentum),
     ML (ml_direction), and BYOC (custom_code with the server template).

Everything except the market-data backfill goes through HTTP — the seed is
also an end-to-end smoke test of auth, orders, and the backtest pipeline.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

# `python scripts/demo_seed.py` puts scripts/ (not the repo root) on sys.path;
# the `app` package lives one level up.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = os.environ.get("BACKEND_URL", "http://localhost:8000")
PASSWORD = "demo-pass-123"
USERS = [
    {"email": "alice@demo.backtester.dev", "username": "alice_demo"},
    {"email": "bob@demo.backtester.dev", "username": "bob_demo"},
]


# ----------------------------------------------------------------- http --
def call(path: str, method: str = "GET", body: dict | None = None,
         token: str | None = None) -> dict | list:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        BASE + path, method=method, headers=headers,
        data=json.dumps(body).encode() if body is not None else None,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode(), strict=False)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:300]
        raise SystemExit(f"FATAL {method} {path} -> {exc.code}: {detail}") from exc


def wait_for_api(timeout_s: int = 90) -> None:
    print(f"waiting for API at {BASE} ...", end="", flush=True)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            urllib.request.urlopen(BASE + "/health", timeout=3)
            print(" up")
            return
        except Exception:
            time.sleep(2)
            print(".", end="", flush=True)
    raise SystemExit(f"\nFATAL API not reachable at {BASE} after {timeout_s}s")


def login_or_register(u: dict) -> str:
    body = {"email": u["email"], "password": PASSWORD}
    try:
        return call("/auth/login", "POST", body)["access_token"]  # type: ignore[index]
    except SystemExit:
        call("/auth/register", "POST", {**body, "username": u["username"]})
        return call("/auth/login", "POST", body)["access_token"]  # type: ignore[index]


# ----------------------------------------------------------------- data --
def backfill_market_data() -> dict[str, int]:
    """The full universe (NASDAQ-100 + NIFTY 50 + top crypto, incl. intraday)
    via scripts/backfill_universe.py. Returns the asset ids the demo trades."""
    from sqlalchemy import select

    from app.db.session import SessionLocal
    from scripts.backfill_universe import main as backfill_universe

    backfill_universe()

    from app.models import Asset
    ids: dict[str, int] = {}
    with SessionLocal() as db:
        for symbol in ("AAPL", "MSFT", "BTCUSDT"):
            ids[symbol] = db.scalar(select(Asset.id).where(Asset.symbol == symbol))
            if ids[symbol] is None:
                raise SystemExit(f"FATAL {symbol} missing after universe backfill — "
                                 "network blocked? Try again or check egress.")
    return ids


# ------------------------------------------------------------ portfolios --
def ensure_portfolio(token: str, name: str, cash: str) -> str:
    for p in call("/portfolios", token=token):  # type: ignore[union-attr]
        if p["name"] == name:
            return p["id"]
    return call("/portfolios", "POST",
                {"name": name, "initial_cash": cash, "is_public": True},
                token=token)["id"]  # type: ignore[index]


def trade(token: str, pid: str, asset_id: int, side: str, qty: str) -> None:
    r = call(f"/portfolios/{pid}/orders", "POST",
             {"asset_id": asset_id, "side": side, "qty": qty}, token=token)
    status = r["status"]  # type: ignore[index]
    px = r.get("fill_price")  # type: ignore[union-attr]
    print(f"    {side:>4} {qty:>6} asset#{asset_id}: {status}"
          + (f" @ ${float(px):,.2f}" if px else f" ({r.get('reason')})"))  # type: ignore[union-attr]


# ------------------------------------------------------------- backtests --
def run_backtest(token: str, label: str, payload: dict, timeout_s: int = 300) -> None:
    # strategies are unique per (user, name) — timestamp the name so re-runs
    # create a fresh version instead of tripping the constraint
    sv = call("/strategies", "POST",
              {"name": f"demo {label} {int(time.time())}", "code": payload.get("code", "")},
              token=token)
    bt = call("/backtests", "POST",
              {"strategy_version_id": sv["version_id"], "timeframe": "1d",  # type: ignore[index]
               "initial_capital": 100_000, "commission_bps": 5, "n_trials": 20,
               **payload},
              token=token)
    bt_id = bt["id"]  # type: ignore[index]
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        d = call(f"/backtests/{bt_id}", token=token)
        if d["status"] in ("completed", "failed"):  # type: ignore[index]
            break
        time.sleep(3)
    status = d["status"]  # type: ignore[index]
    if status != "completed":
        raise SystemExit(f"FATAL backtest {label} ended {status}: {d.get('error')}")  # type: ignore[union-attr]
    print(f"    {label:<28} return {float(d['total_return_pct'] or 0):>8.2f}%  "  # type: ignore[index]
          f"sharpe {float(d['sharpe'] or 0):>5.2f}  "
          f"deflated {float(d['deflated_sharpe'] or 0):>5.2f}")


def main() -> None:
    wait_for_api()

    print("1/4 market data (real history):")
    ids = backfill_market_data()
    aapl, msft, btc = ids["AAPL"], ids["MSFT"], ids["BTCUSDT"]

    print("2/4 demo users + shared public portfolios:")
    alice = login_or_register(USERS[0])
    bob = login_or_register(USERS[1])
    p_alice = ensure_portfolio(alice, "Alpha Capital", "100000.00")
    p_bob = ensure_portfolio(bob, "Beta Fund", "100000.00")
    print(f"  alice_demo -> Alpha Capital ({p_alice[:8]})")
    print(f"  bob_demo   -> Beta Fund     ({p_bob[:8]})")

    print("3/4 trades through the order API:")
    if call(f"/portfolios/{p_alice}/positions", token=alice):
        print("    positions already exist — skipping (idempotent re-run)")
    else:
        trade(alice, p_alice, aapl, "buy", "60")
        trade(alice, p_alice, msft, "buy", "30")
        trade(alice, p_alice, aapl, "sell", "15")   # realized P&L on the books
        trade(bob, p_bob, btc, "buy", "0.4")
        trade(bob, p_bob, aapl, "buy", "25")

    print("4/4 one backtest of each kind (through the Celery worker):")
    template = call("/strategies/registry", token=alice)["custom_template"]  # type: ignore[index]
    run_backtest(alice, "classic: sma_crossover", {
        "strategy": "sma_crossover", "asset_id": aapl,
        "params": {"fast": 20, "slow": 50}})
    run_backtest(alice, "portfolio: cs_momentum", {
        "strategy": "cross_sectional_momentum", "asset_ids": [aapl, msft],
        "params": {"lookback": 60}, "borrow_bps_annual": 50, "max_gross_leverage": 2})
    run_backtest(alice, "ml: ml_direction", {
        "strategy": "ml_direction", "asset_id": aapl, "params": {}})
    run_backtest(alice, "byoc: custom_code", {
        "strategy": "custom_code", "asset_id": msft, "code": template, "params": {}})

    front = os.environ.get("FRONTEND_URL", "http://localhost:3000")
    print("\n✔ demo ready")
    print(f"  app:       {front}   (login: {USERS[0]['email']} / {PASSWORD})")
    print(f"  grafana:   http://localhost:3001/d/backtester-main  (admin/admin)")
    print(f"  prometheus http://localhost:9090/alerts")


if __name__ == "__main__":
    sys.exit(main())
