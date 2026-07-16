"""Portfolio ledger + shared-cash execution tests.

The headline is test_concurrent_orders_cannot_double_spend: two threads
race to spend a shared balance that only covers one order; the
SELECT-FOR-UPDATE executor must let exactly one through and never let cash
go negative. This is the core multiplayer correctness guarantee.
"""
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import pytest
from sqlalchemy import delete

from app.core.security import create_access_token
from app.db.session import SessionLocal
from app.models import (
    Asset,
    LedgerEntry,
    OhlcvBar,
    Order,
    Portfolio,
    PortfolioMember,
    Position,
    Trade,
    User,
)
from app.models.enums import AssetClass, OrderSide, OrderStatus, PortfolioRole, Timeframe
from app.services.execution import execute_market_order


@pytest.fixture()
def env():
    """A funded portfolio (owner) + an asset priced at 100. Torn down after."""
    from datetime import datetime, timezone
    with SessionLocal() as db:
        user = User(email=f"pf_{uuid.uuid4().hex[:8]}@e.com",
                    username=f"pf_{uuid.uuid4().hex[:8]}", hashed_password="x")
        asset = Asset(symbol=f"T{uuid.uuid4().hex[:6].upper()}", exchange="BINANCE",
                      asset_class=AssetClass.CRYPTO)
        db.add_all([user, asset]); db.commit(); db.refresh(user); db.refresh(asset)
        db.add(OhlcvBar(asset_id=asset.id, timeframe=Timeframe.M1,
                        time=datetime(2025, 6, 1, tzinfo=timezone.utc),
                        open=100, high=100, low=100, close=100, volume=1))
        p = Portfolio(name="shared", owner_id=user.id, initial_cash=Decimal("1000"),
                      cash_balance=Decimal("1000"))
        db.add(p); db.commit(); db.refresh(p)
        db.add(PortfolioMember(portfolio_id=p.id, user_id=user.id, role=PortfolioRole.OWNER))
        db.commit()
        ctx = {"user_id": user.id, "asset_id": asset.id, "portfolio_id": p.id,
               "token": create_access_token(user.id)}
    yield ctx
    with SessionLocal() as db:
        pid, aid, uid = ctx["portfolio_id"], ctx["asset_id"], ctx["user_id"]
        db.execute(delete(LedgerEntry).where(LedgerEntry.portfolio_id == pid))
        db.execute(delete(Trade).where(Trade.portfolio_id == pid))
        db.execute(delete(Order).where(Order.portfolio_id == pid))
        db.execute(delete(Position).where(Position.portfolio_id == pid))
        db.execute(delete(PortfolioMember).where(PortfolioMember.portfolio_id == pid))
        db.execute(delete(Portfolio).where(Portfolio.id == pid))
        db.execute(delete(OhlcvBar).where(OhlcvBar.asset_id == aid))
        db.execute(delete(Asset).where(Asset.id == aid))
        db.execute(delete(User).where(User.id == uid))
        db.commit()


# ----------------------------------------------------------- unit: executor --
def test_buy_updates_cash_position_ledger_and_version(env):
    with SessionLocal() as db:
        res = execute_market_order(db, portfolio_id=env["portfolio_id"],
                                   user_id=env["user_id"], asset_id=env["asset_id"],
                                   side=OrderSide.BUY, qty=Decimal("3"))
    assert res.filled
    assert res.fill_price == Decimal("100.00000000")
    assert res.cash_balance == Decimal("700.00")   # 1000 - 3*100
    assert res.version == 1

    with SessionLocal() as db:
        pos = db.get(Position, (env["portfolio_id"], env["asset_id"]))
        assert pos.qty == Decimal("3")
        assert pos.avg_entry_price == Decimal("100.00000000")
        # ledger balance_after must equal the portfolio cash (self-verifying)
        entry = db.scalars(
            db.query(LedgerEntry).filter_by(portfolio_id=env["portfolio_id"]).statement
        ).all()[-1]
        assert entry.balance_after == Decimal("700.00")


def test_insufficient_funds_rejected_not_negative(env):
    with SessionLocal() as db:
        res = execute_market_order(db, portfolio_id=env["portfolio_id"],
                                   user_id=env["user_id"], asset_id=env["asset_id"],
                                   side=OrderSide.BUY, qty=Decimal("50"))  # 5000 > 1000
    assert res.status == OrderStatus.REJECTED
    assert "insufficient funds" in res.reason
    assert res.cash_balance == Decimal("1000")     # untouched
    assert res.version == 0


def test_sell_without_position_opens_short(env):
    """Shorting is enabled: a sell with nothing held opens a negative position
    and credits the proceeds to cash (rather than rejecting)."""
    with SessionLocal() as db:
        res = execute_market_order(db, portfolio_id=env["portfolio_id"],
                                   user_id=env["user_id"], asset_id=env["asset_id"],
                                   side=OrderSide.SELL, qty=Decimal("1"))
    assert res.filled
    assert res.cash_balance == Decimal("1100.00")     # 1000 + 100 short proceeds
    with SessionLocal() as db:
        pos = db.get(Position, (env["portfolio_id"], env["asset_id"]))
        assert pos.qty == Decimal("-1")               # negative = short
        assert pos.avg_entry_price == Decimal("100")


def test_buy_then_sell_realizes_pnl(env):
    with SessionLocal() as db:
        execute_market_order(db, portfolio_id=env["portfolio_id"], user_id=env["user_id"],
                             asset_id=env["asset_id"], side=OrderSide.BUY, qty=Decimal("4"))
    # bump the market price to 120, then sell 4
    from datetime import datetime, timezone
    with SessionLocal() as db:
        db.add(OhlcvBar(asset_id=env["asset_id"], timeframe=Timeframe.M1,
                        time=datetime(2025, 6, 2, tzinfo=timezone.utc),
                        open=120, high=120, low=120, close=120, volume=1))
        db.commit()
    with SessionLocal() as db:
        res = execute_market_order(db, portfolio_id=env["portfolio_id"], user_id=env["user_id"],
                                   asset_id=env["asset_id"], side=OrderSide.SELL, qty=Decimal("4"))
    assert res.filled
    # 1000 - 400 (buy) + 480 (sell) = 1080
    assert res.cash_balance == Decimal("1080.00")
    with SessionLocal() as db:
        pos = db.get(Position, (env["portfolio_id"], env["asset_id"]))
        assert pos.qty == Decimal("0")
        assert pos.realized_pnl == Decimal("80.00")   # (120-100)*4


# ----------------------------------------------- THE HEADLINE: concurrency --
def test_concurrent_orders_cannot_double_spend(env):
    """Two simultaneous buys of 1500 each against 1000 cash at 2x leverage
    (buying power 2000). Each fits alone, but not together — exactly one must
    fill. The SELECT ... FOR UPDATE lock serializes the buying-power check, so
    the second sees the first's spend and is rejected."""
    barrier = Barrier(2)

    def _place():
        barrier.wait()  # maximize contention: both hit the lock together
        with SessionLocal() as db:
            return execute_market_order(
                db, portfolio_id=env["portfolio_id"], user_id=env["user_id"],
                asset_id=env["asset_id"], side=OrderSide.BUY, qty=Decimal("15"),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result() for f in [pool.submit(_place), pool.submit(_place)]]

    filled = [r for r in results if r.filled]
    rejected = [r for r in results if not r.filled]
    assert len(filled) == 1, "exactly one order may fill"
    assert len(rejected) == 1
    assert "insufficient funds" in rejected[0].reason

    with SessionLocal() as db:
        p = db.get(Portfolio, env["portfolio_id"])
        # 1000 - 1500 = -500: negative is legitimate margin now, but only ONE
        # order's worth — the second could never double-spend the buying power.
        assert p.cash_balance == Decimal("-500.00")
        assert p.version == 1                          # only the fill bumped it
        trades = db.query(Trade).filter_by(portfolio_id=env["portfolio_id"]).count()
        assert trades == 1
