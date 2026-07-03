"""Order idempotency — exactly-once fills under client retries.

Two proofs:
  - HTTP double-submit with the same key ⇒ one Order row, identical response,
    cash moved once (the retry-after-401 / double-click path).
  - True concurrency: N threads on separate Postgres connections submit the
    SAME key at once ⇒ exactly one fill, cash debited once. This exercises the
    real SELECT-FOR-UPDATE serialization + the unique-constraint backstop.
"""
import threading
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select

from app.db.session import SessionLocal
from app.models import Asset, LedgerEntry, OhlcvBar, Order, Portfolio, Position, Trade, User
from app.models.enums import AssetClass, OrderSide, OrderStatus, Timeframe
from app.services.execution import execute_market_order


def _register(client, tag: str) -> dict:
    s = uuid.uuid4().hex[:10]
    email = f"idem_{tag}_{s}@example.com"
    client.post("/auth/register", json={
        "email": email, "username": f"idem_{tag}_{s}", "password": "s3cret-pass!"})
    tok = client.post("/auth/login", json={
        "email": email, "password": "s3cret-pass!"}).json()["access_token"]
    return {"headers": {"Authorization": f"Bearer {tok}"}, "email": email}


@pytest.fixture()
def idem_env(client):
    user = _register(client, "u")
    with SessionLocal() as db:
        asset = Asset(symbol=f"ID{uuid.uuid4().hex[:6].upper()}", exchange="TEST",
                      asset_class=AssetClass.CRYPTO)
        db.add(asset); db.commit(); db.refresh(asset)
        db.add(OhlcvBar(asset_id=asset.id, timeframe=Timeframe.M1,
                        time=datetime(2025, 6, 1, tzinfo=timezone.utc),
                        open=100, high=100, low=100, close=100, volume=1))
        db.commit()
        asset_id = asset.id
    r = client.post("/portfolios", headers=user["headers"],
                    json={"name": f"idem {uuid.uuid4().hex[:6]}", "initial_cash": "10000.00"})
    pid = r.json()["id"]
    with SessionLocal() as db:
        owner_id = db.scalar(select(Portfolio.owner_id).where(Portfolio.id == uuid.UUID(pid)))
    yield {"user": user, "asset_id": asset_id, "pid": pid, "owner_id": owner_id}
    with SessionLocal() as db:
        pu = uuid.UUID(pid)
        db.execute(delete(LedgerEntry).where(LedgerEntry.portfolio_id == pu))
        db.execute(delete(Trade).where(Trade.portfolio_id == pu))
        db.execute(delete(Order).where(Order.portfolio_id == pu))
        db.execute(delete(Position).where(Position.portfolio_id == pu))
        from app.models import PortfolioMember
        db.execute(delete(PortfolioMember).where(PortfolioMember.portfolio_id == pu))
        db.execute(delete(Portfolio).where(Portfolio.id == pu))
        db.execute(delete(OhlcvBar).where(OhlcvBar.asset_id == asset_id))
        db.execute(delete(Asset).where(Asset.id == asset_id))
        db.execute(delete(User).where(User.email == user["email"]))
        db.commit()


def _order_count(pid: str) -> int:
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(Order)
                         .where(Order.portfolio_id == uuid.UUID(pid)))


def test_http_double_submit_fills_once(client, idem_env):
    headers, aid, pid = idem_env["user"]["headers"], idem_env["asset_id"], idem_env["pid"]
    key = uuid.uuid4().hex
    body = {"asset_id": aid, "side": "buy", "qty": "5", "idempotency_key": key}

    r1 = client.post(f"/portfolios/{pid}/orders", headers=headers, json=body)
    r2 = client.post(f"/portfolios/{pid}/orders", headers=headers, json=body)
    assert r1.status_code == 200 and r2.status_code == 200, (r1.text, r2.text)
    a, b = r1.json(), r2.json()

    assert a["status"] == "filled" and b["status"] == "filled"
    assert a["order_id"] == b["order_id"]          # same logical order
    assert a["cash_balance"] == b["cash_balance"]  # cash moved once
    assert Decimal(a["cash_balance"]) == Decimal("9500.00")  # 10000 - 5*100
    assert _order_count(pid) == 1                  # exactly one row

    # A DIFFERENT key is a new order.
    r3 = client.post(f"/portfolios/{pid}/orders", headers=headers,
                     json={**body, "idempotency_key": uuid.uuid4().hex})
    assert r3.json()["status"] == "filled"
    assert _order_count(pid) == 2


def test_concurrent_same_key_fills_once(idem_env):
    """8 threads, separate Postgres connections, same key, at once."""
    pid = uuid.UUID(idem_env["pid"])
    aid, owner = idem_env["asset_id"], idem_env["owner_id"]
    key = uuid.uuid4().hex
    barrier = threading.Barrier(8)
    results: list = []
    lock = threading.Lock()

    def submit():
        barrier.wait()  # maximize contention
        with SessionLocal() as db:
            res = execute_market_order(
                db, portfolio_id=pid, user_id=owner, asset_id=aid,
                side=OrderSide.BUY, qty=Decimal("5"), idempotency_key=key)
        with lock:
            results.append(res)

    threads = [threading.Thread(target=submit) for _ in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert len(results) == 8
    order_ids = {r.order_id for r in results}
    assert len(order_ids) == 1, f"expected one logical order, got {order_ids}"
    assert all(r.status == OrderStatus.FILLED for r in results)

    with SessionLocal() as db:
        n_orders = db.scalar(select(func.count()).select_from(Order)
                             .where(Order.portfolio_id == pid))
        pf = db.get(Portfolio, pid)
    assert n_orders == 1                            # only ONE row despite 8 threads
    assert pf.cash_balance == Decimal("9500.00")    # debited exactly once
