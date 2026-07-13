"""Multi-currency ledger correctness: an NSE fill at ₹P must debit USD P/fx,
valuations must convert marks, and a missing rate must REJECT (never guess)."""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from app.core.security import create_access_token, hash_password
from app.db.session import SessionLocal
from app.models import (
    Asset, FxRate, LedgerEntry, OhlcvBar, Order, Portfolio, PortfolioMember,
    Position, Trade, User,
)
from app.models.enums import AssetClass, Timeframe

_PW = hash_password("s3cret-pass!")
RATE = Decimal("100")          # 100 INR per USD — clean arithmetic on purpose
PRICE_INR = Decimal("2500")    # asset close in rupees


@pytest.fixture()
def fx_env(client):
    s = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        # Preserve whatever real USDINR the beat has stored; tests pin 100.
        prior = db.get(FxRate, "USDINR")
        prior_rate = Decimal(prior.rate) if prior else None
        if prior is None:
            db.add(FxRate(pair="USDINR", rate=RATE))
        else:
            prior.rate = RATE
        u = User(email=f"fx_{s}@x.com", username=f"fx_{s}", hashed_password=_PW)
        a = Asset(symbol=f"FX{s[:5].upper()}", exchange="NSE",
                  asset_class=AssetClass.IN_EQUITY, currency="INR")
        db.add_all([u, a]); db.commit(); db.refresh(u); db.refresh(a)
        db.add(OhlcvBar(asset_id=a.id, timeframe=Timeframe.D1,
                        time=datetime(2026, 7, 1, tzinfo=timezone.utc),
                        open=PRICE_INR, high=PRICE_INR, low=PRICE_INR,
                        close=PRICE_INR, volume=1000))
        db.commit()
        env = {"uid": u.id, "aid": a.id,
               "h": {"Authorization": f"Bearer {create_access_token(u.id)}"}}
    pid = client.post("/portfolios", headers=env["h"],
                      json={"name": "fx fund", "initial_cash": "1000.00"}).json()["id"]
    env["pid"] = pid
    yield env
    with SessionLocal() as db:
        pu = uuid.UUID(pid)
        db.execute(delete(LedgerEntry).where(LedgerEntry.portfolio_id == pu))
        db.execute(delete(Trade).where(Trade.portfolio_id == pu))
        db.execute(delete(Order).where(Order.portfolio_id == pu))
        db.execute(delete(Position).where(Position.portfolio_id == pu))
        db.execute(delete(PortfolioMember).where(PortfolioMember.portfolio_id == pu))
        db.execute(delete(Portfolio).where(Portfolio.id == pu))
        db.execute(delete(OhlcvBar).where(OhlcvBar.asset_id == env["aid"]))
        db.execute(delete(Asset).where(Asset.id == env["aid"]))
        db.execute(delete(User).where(User.id == env["uid"]))
        row = db.get(FxRate, "USDINR")
        if prior_rate is not None and row is not None:
            row.rate = prior_rate          # restore the beat's real rate
        elif prior_rate is None and row is not None:
            db.delete(row)
        db.commit()


def test_inr_fill_debits_converted_usd(client, fx_env):
    r = client.post(f"/portfolios/{fx_env['pid']}/orders", headers=fx_env["h"],
                    json={"asset_id": fx_env["aid"], "side": "buy", "qty": "1"}).json()
    assert r["status"] == "filled"
    # fill price stays in the ASSET currency
    assert Decimal(r["fill_price"]) == PRICE_INR
    # cash debited in USD: 2500/100 = 25.00 + commission bps on 25.00
    cash = Decimal(r["cash_balance"])
    assert Decimal("974") < cash < Decimal("975.01")
    with SessionLocal() as db:
        entry = db.execute(select(LedgerEntry).where(
            LedgerEntry.portfolio_id == uuid.UUID(fx_env["pid"]),
            LedgerEntry.entry_type != "deposit").order_by(LedgerEntry.id.desc())
        ).scalars().first()
        assert entry.amount <= Decimal("-25.00")          # converted, not raw ₹
        assert entry.amount > Decimal("-26")              # ...and not ₹2500-as-$
        assert "INR" in entry.note and "USDINR" in entry.note  # rate audited
        pos = db.get(Position, (uuid.UUID(fx_env["pid"]), fx_env["aid"]))
        assert pos.avg_entry_price == PRICE_INR           # asset-currency basis


def test_valuations_convert_inr_marks(client, fx_env):
    client.post(f"/portfolios/{fx_env['pid']}/orders", headers=fx_env["h"],
                json={"asset_id": fx_env["aid"], "side": "buy", "qty": "1"})
    from app.services.snapshots import snapshot_portfolio_equity
    with SessionLocal() as db:
        snapshot_portfolio_equity(db)
        from app.models import PortfolioEquitySnapshot as Snap
        snap = db.execute(select(Snap).where(
            Snap.portfolio_id == uuid.UUID(fx_env["pid"]))
            .order_by(Snap.time.desc())).scalars().first()
        # equity = cash (USD) + 2500/100 = cash + 25 — NOT cash + 2500
        assert snap.equity - snap.cash == Decimal("25.00")

    # ledger replay terminal mark converts identically
    from app.services.equity import equity_histories
    with SessionLocal() as db:
        curve = equity_histories(db, [uuid.UUID(fx_env["pid"])])[uuid.UUID(fx_env["pid"])]
        assert curve[-1].equity - curve[-1].cash == Decimal("25.00")


def test_missing_fx_rate_rejects_trade(client, fx_env):
    with SessionLocal() as db:
        row = db.get(FxRate, "USDINR")
        db.delete(row); db.commit()
    try:
        r = client.post(f"/portfolios/{fx_env['pid']}/orders", headers=fx_env["h"],
                        json={"asset_id": fx_env["aid"], "side": "buy", "qty": "1"})
        assert r.status_code == 422
        assert "FX rate" in r.json()["detail"]
    finally:  # restore for the fixture's teardown bookkeeping
        with SessionLocal() as db:
            db.add(FxRate(pair="USDINR", rate=RATE)); db.commit()
