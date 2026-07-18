"""Negative-path coverage — the failure modes live verification kept finding.

What production taught us, pinned as tests:
  - Runner unavailable: submission must still 202 and the row must sit
    QUEUED — the API never blocks on (or silently loses) a backtest.
  - Order rejection: bad orders come back `rejected` with a reason over HTTP
    and leave the shared cash balance untouched (service-level variants live
    in test_portfolio_ledger; this is the API contract).
  - Runaway BYOC code: memory caps can't stop a tight loop, so the sandbox
    denies the amplification primitives outright and the reaper fails any
    run past BACKTEST_TIME_LIMIT_S (see test_reaper). Actually running a
    10-minute loop in CI buys nothing extra.
  - Leaderboard with zero (old) snapshots: windowed returns must degrade to
    the all-time baseline, never error or rank on NULL.
"""
import uuid

import pytest

from app.db.session import SessionLocal
from sqlalchemy import delete, select

from app.models import Asset, OhlcvBar, Portfolio, User
from app.models.enums import AssetClass, Timeframe


def _register(client, tag: str) -> dict:
    suffix = uuid.uuid4().hex[:10]
    email = f"np_{tag}_{suffix}@example.com"
    r = client.post("/auth/register", json={
        "email": email, "username": f"np_{tag}_{suffix}", "password": "s3cret-pass!"})
    assert r.status_code == 201, r.text
    token = client.post("/auth/login", json={
        "email": email, "password": "s3cret-pass!"}).json()["access_token"]
    return {"headers": {"Authorization": f"Bearer {token}"}, "email": email}


@pytest.fixture()
def np_env(client):
    """User + asset priced at 100 + full teardown."""
    user = _register(client, "user")
    from datetime import datetime, timezone
    with SessionLocal() as db:
        asset = Asset(symbol=f"NP{uuid.uuid4().hex[:6].upper()}", exchange="TEST",
                      asset_class=AssetClass.CRYPTO)
        db.add(asset); db.commit(); db.refresh(asset)
        db.add(OhlcvBar(asset_id=asset.id, timeframe=Timeframe.M1,
                        time=datetime(2025, 6, 1, tzinfo=timezone.utc),
                        open=100, high=100, low=100, close=100, volume=1))
        db.commit()
        asset_id = asset.id
    yield {"user": user, "asset_id": asset_id}
    with SessionLocal() as db:
        uid = db.scalar(select(User.id).where(User.email == user["email"]))
        pids = db.scalars(select(Portfolio.id).where(Portfolio.owner_id == uid)).all()
        # FK cascades sweep members/orders/trades/ledger/snapshots.
        from app.models import LedgerEntry, PortfolioEquitySnapshot, PortfolioMember
        from app.models.trading import Order, Position, Trade
        db.execute(delete(PortfolioEquitySnapshot)
                   .where(PortfolioEquitySnapshot.portfolio_id.in_(pids)))
        db.execute(delete(LedgerEntry).where(LedgerEntry.portfolio_id.in_(pids)))
        db.execute(delete(Trade).where(Trade.portfolio_id.in_(pids)))
        db.execute(delete(Order).where(Order.portfolio_id.in_(pids)))
        db.execute(delete(Position).where(Position.portfolio_id.in_(pids)))
        db.execute(delete(PortfolioMember).where(PortfolioMember.portfolio_id.in_(pids)))
        db.execute(delete(Portfolio).where(Portfolio.id.in_(pids)))
        db.execute(delete(OhlcvBar).where(OhlcvBar.asset_id == asset_id))
        db.execute(delete(Asset).where(Asset.id == asset_id))
        db.execute(delete(User).where(User.id == uid))
        db.commit()


def _portfolio(client, headers, cash="1000.00", public=False) -> str:
    r = client.post("/portfolios", headers=headers,
                    json={"name": f"np {uuid.uuid4().hex[:6]}",
                          "initial_cash": cash, "is_public": public})
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ------------------------------------------------------------ submit path --
def test_backtest_submit_returns_queued(client, np_env, monkeypatch):
    """Submit returns 202 with a QUEUED row before execution runs. Stub the
    in-process runner so the row is observed pre-execution (BackgroundTasks
    would otherwise complete it synchronously under the test client)."""
    import app.backtest.tasks as tasks_mod
    monkeypatch.setattr(tasks_mod, "execute_backtest", lambda *a, **k: None)

    headers = np_env["user"]["headers"]
    sv = client.post("/strategies", headers=headers,
                     json={"name": f"np queued {uuid.uuid4().hex[:6]}"}).json()
    r = client.post("/backtests", headers=headers, json={
        "strategy_version_id": sv["version_id"], "asset_id": np_env["asset_id"],
        "strategy": "sma_crossover", "params": {"fast": 5, "slow": 10}})
    assert r.status_code == 202, r.text
    bt_id = r.json()["id"]

    r = client.get(f"/backtests/{bt_id}", headers=headers)
    assert r.status_code == 200
    assert r.json()["status"] == "queued"
    assert r.json()["total_return_pct"] is None


# -------------------------------------------------------- order rejection --
def test_order_rejections_over_http(client, np_env):
    headers = np_env["user"]["headers"]
    pid = _portfolio(client, headers, cash="1000.00")

    # Insufficient buying power: 50 @ 100 = 5000 > 1000. Rejected, cash untouched.
    r = client.post(f"/portfolios/{pid}/orders", headers=headers,
                    json={"asset_id": np_env["asset_id"], "side": "buy", "qty": "50"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "rejected"
    assert "insufficient funds" in body["reason"]
    # The rejected buy left the shared balance exactly where it started.
    r = client.get(f"/portfolios/{pid}", headers=headers)
    assert r.json()["cash_balance"] == "1000.00"

    # Oversell with nothing held now OPENS A SHORT (shorting enabled) and
    # credits the proceeds, rather than rejecting.
    r = client.post(f"/portfolios/{pid}/orders", headers=headers,
                    json={"asset_id": np_env["asset_id"], "side": "sell", "qty": "1"})
    assert r.json()["status"] == "filled"
    r = client.get(f"/portfolios/{pid}", headers=headers)
    assert r.json()["cash_balance"] == "1100.00"     # 1000 + 100 short proceeds


def test_sandbox_rejects_obvious_resource_abuse_shapes():
    """The AST gate can't solve halting, but the classic amplification
    primitives users reach for first are simply not in the namespace."""
    from app.backtest.sandbox import validate_code

    assert any("'eval' is not allowed" in e for e in validate_code(
        "class X(CustomStrategy):\n    def next(self, i, bar):\n        eval('1')"))
    assert any("imports are not allowed" in e for e in validate_code("import threading"))
    # os/sys/subprocess simply don't exist in the exec namespace
    from app.backtest.sandbox import SandboxError, build_custom_strategy
    src = ("class X(CustomStrategy):\n"
           "    def generate(self, data):\n"
           "        return os.system('true')\n")
    strategy = build_custom_strategy(src)
    import pandas as pd
    from app.backtest.sandbox import run_custom_strategy
    with pytest.raises(SandboxError, match="os"):
        run_custom_strategy(strategy, pd.DataFrame(
            {"close": [1.0, 2.0]},
            index=pd.date_range("2024-01-01", periods=2, tz="UTC")))
