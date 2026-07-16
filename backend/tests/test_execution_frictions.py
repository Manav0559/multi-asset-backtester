"""Fill frictions + short margin: slippage worsens both sides of the fill, and
opening a short requires initial margin so short exposure is capped.

The rest of the suite pins SLIPPAGE_BPS=0 (conftest) so its exact-arithmetic
assertions stay meaningful; these tests flip the knobs explicitly.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import delete

from app.core.config import settings
from app.db.session import SessionLocal
from app.models import (
    Asset, LedgerEntry, OhlcvBar, Order, Portfolio, PortfolioMember, Position,
    Trade, User,
)
from app.models.enums import AssetClass, OrderSide, OrderStatus, Timeframe
from app.services.execution import execute_market_order

_PRICE = Decimal("100")


@pytest.fixture()
def fr_env():
    """Funded portfolio + asset priced at 100, mirroring the ledger fixture."""
    with SessionLocal() as db:
        user = User(email=f"fr_{uuid.uuid4().hex[:8]}@e.com",
                    username=f"fr_{uuid.uuid4().hex[:8]}", hashed_password="x")
        asset = Asset(symbol=f"FR{uuid.uuid4().hex[:6].upper()}", exchange="BINANCE",
                      asset_class=AssetClass.CRYPTO)
        db.add_all([user, asset]); db.commit(); db.refresh(user); db.refresh(asset)
        db.add(OhlcvBar(asset_id=asset.id, timeframe=Timeframe.M1,
                        time=datetime(2025, 6, 1, tzinfo=timezone.utc),
                        open=_PRICE, high=_PRICE, low=_PRICE, close=_PRICE, volume=1))
        p = Portfolio(name="frictions", owner_id=user.id,
                      initial_cash=Decimal("10000"), cash_balance=Decimal("10000"))
        db.add(p); db.commit(); db.refresh(p)
        ctx = {"user_id": user.id, "asset_id": asset.id, "portfolio_id": p.id}
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


def _order(env, side, qty):
    with SessionLocal() as db:
        return execute_market_order(db, portfolio_id=env["portfolio_id"],
                                    user_id=env["user_id"], asset_id=env["asset_id"],
                                    side=side, qty=Decimal(qty))


def test_slippage_worsens_both_sides(fr_env, monkeypatch):
    monkeypatch.setattr(settings, "SLIPPAGE_BPS", 10.0)   # 10 bps for clean numbers
    buy = _order(fr_env, OrderSide.BUY, "1")
    assert buy.filled
    assert buy.fill_price == Decimal("100.10000000")      # buys pay up
    sell = _order(fr_env, OrderSide.SELL, "1")
    assert sell.filled
    assert sell.fill_price == Decimal("99.90000000")      # sells receive less
    with SessionLocal() as db:
        p = db.get(Portfolio, fr_env["portfolio_id"])
        # Round trip loses exactly the two slips: 10000 - 100.10 + 99.90
        assert p.cash_balance == Decimal("9999.80")


def test_short_requires_initial_margin(fr_env, monkeypatch):
    monkeypatch.setattr(settings, "SLIPPAGE_BPS", 0.0)
    # 10000 cash, 2x lev -> buying power 20000. 150% initial margin charges
    # 0.5x the short notional: max shortable = 20000/0.5 = 400 @ 100.
    ok = _order(fr_env, OrderSide.SELL, "300")            # needs 15000 <= 20000
    assert ok.filled
    too_big = _order(fr_env, OrderSide.SELL, "5000")      # needs 250000 -> reject
    assert too_big.status == OrderStatus.REJECTED
    assert "insufficient margin for short" in too_big.reason


def test_closing_a_long_needs_no_short_margin(fr_env, monkeypatch):
    monkeypatch.setattr(settings, "SLIPPAGE_BPS", 0.0)
    assert _order(fr_env, OrderSide.BUY, "50").filled
    # Selling what you hold is a close, not a short — no margin check applies,
    # even for a quantity that would fail as a fresh short of the same size.
    close = _order(fr_env, OrderSide.SELL, "50")
    assert close.filled
    with SessionLocal() as db:
        pos = db.get(Position, (fr_env["portfolio_id"], fr_env["asset_id"]))
        assert pos.qty == 0
