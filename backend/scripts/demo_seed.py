"""One-command demo seed — boots a fresh stack into a fully-populated app.

    docker compose --profile app exec backend python scripts/demo_seed.py
    (or ./scripts/demo.sh from the repo root, which wraps exactly that)

To seed a live deployment, point the two envs at the hosted stack:

    DATABASE_URL=<postgres> BACKEND_URL=https://<app>.onrender.com \
        python scripts/demo_seed.py

DATABASE_URL is used only for the direct market-data backfill; everything else
hits BACKEND_URL over HTTP.

What it does, idempotently (safe to re-run):
  1. Backfills REAL history: AAPL + MSFT (5y daily via yfinance) and
     BTCUSDT (1000 daily bars via Binance REST).
  2. Two demo users -> two PUBLIC portfolios -> real trades through the
     order API (so ledgers, positions, and the leaderboard are live).
  3. One backtest of each kind through the real API (FastAPI BackgroundTasks):
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
_ANCHORS = ("AAPL", "MSFT", "BTCUSDT", "ADAUSDT")


def backfill_market_data() -> dict[str, int]:
    """The full universe (NASDAQ-100 + NIFTY 50 + top crypto, incl. intraday)
    via scripts/backfill_universe.py — SKIPPED when the anchors already have
    bars, so re-seeding an existing stack is additive instead of re-walking
    154 symbols (murderously slow on a loaded box). Returns the asset ids the
    demo trades."""
    from sqlalchemy import func, select

    from app.db.session import SessionLocal
    from app.models import Asset, OhlcvBar

    def _resolve(db) -> dict[str, int] | None:
        ids: dict[str, int] = {}
        for symbol in _ANCHORS:
            aid = db.scalar(select(Asset.id).where(Asset.symbol == symbol))
            if aid is None or not db.scalar(
                    select(func.count()).select_from(OhlcvBar)
                    .where(OhlcvBar.asset_id == aid)):
                return None
            ids[symbol] = aid
        return ids

    with SessionLocal() as db:
        ids = _resolve(db)
    if ids is not None:
        print("    universe already present — skipping backfill (additive mode)")
        return ids

    from scripts.backfill_universe import main as backfill_universe
    backfill_universe()
    with SessionLocal() as db:
        ids = _resolve(db)
    if ids is None:
        raise SystemExit("FATAL anchor symbols missing after universe backfill — "
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

    print("1/6 market data (real history):")
    ids = backfill_market_data()
    aapl, msft, btc, ada = ids["AAPL"], ids["MSFT"], ids["BTCUSDT"], ids["ADAUSDT"]

    print("2/6 demo users + shared public portfolios:")
    alice = login_or_register(USERS[0])
    bob = login_or_register(USERS[1])
    p_alice = ensure_portfolio(alice, "Alpha Capital", "100000.00")
    p_bob = ensure_portfolio(bob, "Beta Fund", "100000.00")
    print(f"  alice_demo -> Alpha Capital ({p_alice[:8]})")
    print(f"  bob_demo   -> Beta Fund     ({p_bob[:8]})")

    print("3/6 trades through the order API:")
    if call(f"/portfolios/{p_alice}/positions", token=alice):
        print("    positions already exist — skipping (idempotent re-run)")
    else:
        trade(alice, p_alice, aapl, "buy", "60")
        trade(alice, p_alice, msft, "buy", "30")
        trade(alice, p_alice, aapl, "sell", "15")   # realized P&L on the books
        trade(bob, p_bob, btc, "buy", "0.4")
        trade(bob, p_bob, aapl, "buy", "25")

    print("4/6 one backtest of each kind (through the Celery worker):")
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

    print("5/6 social: shared portfolio + chat + a live competition:")
    if call("/challenges", token=alice):
        print("    social data already present — skipping (idempotent re-run)")
        p_shared = ensure_portfolio(alice, "The Syndicate", "50000.00")
    else:
        # A shared portfolio both trade in (invite -> accept), with team chat.
        p_shared = ensure_portfolio(alice, "The Syndicate", "50000.00")
        inv = call(f"/portfolios/{p_shared}/invites", "POST",
                   {"invitee_email": USERS[1]["email"], "role": "trader"}, token=alice)
        call("/portfolios/invites/accept", "POST", {"token": inv["token"]}, token=bob)  # type: ignore[index]
        for who, body in [(alice, "gm team 👋"), (bob, "morning — AAPL looks strong"),
                          (alice, "agreed, adding on the dip"),
                          (bob, "I'll take the momentum side of the comp")]:
            call(f"/portfolios/{p_shared}/chat", "POST", {"body": body}, token=who)
        print(f"    The Syndicate ({p_shared[:8]}) — 2 members, {4} chat messages")

        # A consent-based head-to-head: Alpha Capital vs Beta Fund, 1 week.
        ch = call("/challenges", "POST", {
            "opponent_username": USERS[1]["username"],
            "challenger_portfolio_id": p_alice, "duration_days": 7}, token=alice)
        call(f"/challenges/{ch['id']}/accept", "POST",  # type: ignore[index]
             {"opponent_portfolio_id": p_bob}, token=bob)
        print(f"    competition active: alice_demo (Alpha) vs bob_demo (Beta), 7d")

    # A LIVE-ticking crypto position in the shared book, so the portfolio and
    # dashboard demo real streamed prices (ADA is the liveliest small-price
    # symbol — sub-dollar moves are visible at 5-decimal precision).
    positions = call(f"/portfolios/{p_shared}/positions", token=alice)
    if any(p["asset_id"] == ada for p in positions):  # type: ignore[union-attr]
        print("    live-symbol position already present — skipping")
    else:
        trade(alice, p_shared, ada, "buy", "100")

    print("6/6 ML catalog — one backtest per family (skips families already run):")
    done = {(b.get("config") or {}).get("strategy")
            for b in call("/backtests", token=alice)  # type: ignore[union-attr]
            if b["status"] == "completed"}
    for fam in ("ml_logistic_regression", "ml_decision_tree", "ml_random_forest",
                "ml_extra_trees", "ml_gradient_boosting", "ml_mlp", "ml_xgboost"):
        if fam in done:
            print(f"    {fam:<28} already completed — skipping")
            continue
        run_backtest(alice, f"ml: {fam}", {"strategy": fam, "asset_id": aapl,
                                           "params": {}}, timeout_s=600)

    front = os.environ.get("FRONTEND_URL", "http://localhost:3000")
    print("\n✔ demo ready")
    print(f"  app:       {front}   (login: {USERS[0]['email']} / {PASSWORD})")
    print(f"  grafana:   http://localhost:3001/d/backtester-main  (admin/admin)")
    print(f"  prometheus http://localhost:9090/alerts")


if __name__ == "__main__":
    sys.exit(main())
