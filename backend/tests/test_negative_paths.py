"""Negative-path coverage — the failure modes live verification kept finding.

What production taught us, pinned as tests:
  - Worker down: submission must still 202 and the row must sit QUEUED —
    the API never blocks on (or silently loses) a backtest.
  - Order rejection: bad orders come back `rejected` with a reason over HTTP
    and leave the shared cash balance untouched (service-level variants live
    in test_portfolio_ledger; this is the API contract).
  - Runaway BYOC code: an infinite loop is unkillable by memory caps —
    RLIMIT_AS bounds address space, not time. The Celery hard time limit is
    the kill switch (prefork SIGKILLs the child; run_and_persist's except
    path marks the row FAILED). Asserted at config level because actually
    running a 10-minute loop in CI buys nothing extra.
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


# ------------------------------------------------------------ worker down --
def test_backtest_stays_queued_when_worker_down(client, np_env, monkeypatch):
    """delay() publishing into a broker with no consumer must not change API
    behavior: 202 on submit, row visible and QUEUED on read, no blocking."""
    from app.backtest.tasks import run_backtest_task
    monkeypatch.setattr(run_backtest_task, "delay", lambda *a, **k: None)

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

    # Insufficient cash: 50 @ 100 = 5000 > 1000. Rejected, cash untouched.
    r = client.post(f"/portfolios/{pid}/orders", headers=headers,
                    json={"asset_id": np_env["asset_id"], "side": "buy", "qty": "50"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "rejected"
    assert "insufficient funds" in body["reason"]

    # Oversell: nothing held. Rejected (no shorting by default).
    r = client.post(f"/portfolios/{pid}/orders", headers=headers,
                    json={"asset_id": np_env["asset_id"], "side": "sell", "qty": "1"})
    assert r.json()["status"] == "rejected"
    assert "insufficient position" in r.json()["reason"]

    # Both rejections left the shared balance exactly where it started.
    r = client.get(f"/portfolios/{pid}", headers=headers)
    assert r.json()["cash_balance"] == "1000.00"


# ------------------------------------------------------------ runaway BYOC --
def test_runaway_custom_code_has_a_kill_switch():
    """An infinite `while True` in user code survives every memory cap —
    RLIMIT_AS bounds address space, not CPU time. The enforced backstop is
    Celery's task_time_limit: the prefork parent SIGKILLs the child at the
    hard limit and the task is recorded failed (run_and_persist marks the
    row FAILED via its except path on the soft limit)."""
    from app.backtest.tasks import celery_app
    from app.core.config import settings

    assert celery_app.conf.task_time_limit == settings.BACKTEST_TIME_LIMIT_S
    assert 0 < celery_app.conf.task_soft_time_limit < celery_app.conf.task_time_limit


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


# ----------------------------------------------------- zero-snapshot board --
def test_windowed_leaderboard_without_snapshots_degrades_to_all_time(client, np_env):
    """A brand-new public portfolio has no snapshot at or before the window
    cutoff (the beat may write CURRENT ones — those are > cutoff and must be
    ignored). Windowed return falls back to the initial-cash baseline and the
    board must not error or produce NULL ranks."""
    headers = np_env["user"]["headers"]
    pid = _portfolio(client, headers, cash="1000.00", public=True)

    results = {}
    for window in ("all", "24h", "7d"):
        r = client.get(f"/leaderboard?limit=100&window={window}", headers=headers)
        assert r.status_code == 200, r.text
        entry = next(e for e in r.json() if e["portfolio_id"] == pid)
        assert entry["rank"] >= 1
        results[window] = entry["return_pct"]

    assert results["24h"] == results["all"]
    assert results["7d"] == results["all"]
