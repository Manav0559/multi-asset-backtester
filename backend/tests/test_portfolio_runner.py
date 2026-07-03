"""Runner integration for the long/short multi-asset engine: a portfolio
strategy submitted as a Backtest row runs through _execute and persists
headline metrics + market-neutrality diagnostics, exactly like the
single-asset path."""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import delete

from app.backtest.runner import run_and_persist
from app.db.session import SessionLocal
from app.models import (
    Asset, Backtest, BacktestYearlyResult, OhlcvBar, Strategy, StrategyVersion, User,
)
from app.models.enums import AssetClass, BacktestStatus, Timeframe


@pytest.fixture()
def two_asset_env():
    """Two assets with 200 aligned daily bars: A trends up, B trends down —
    so a dollar-neutral long-A/short-B book should make money."""
    with SessionLocal() as db:
        u = User(email=f"pf_{uuid.uuid4().hex[:8]}@e.com",
                 username=f"pf_{uuid.uuid4().hex[:8]}", hashed_password="x")
        a = Asset(symbol=f"UP{uuid.uuid4().hex[:5]}", exchange="X", asset_class=AssetClass.US_EQUITY)
        b = Asset(symbol=f"DN{uuid.uuid4().hex[:5]}", exchange="X", asset_class=AssetClass.US_EQUITY)
        db.add_all([u, a, b]); db.commit(); db.refresh(u); db.refresh(a); db.refresh(b)
        t0 = datetime(2020, 1, 1, tzinfo=timezone.utc)
        rows = []
        for i in range(200):
            ts = t0 + timedelta(days=i)
            rows.append(OhlcvBar(asset_id=a.id, timeframe=Timeframe.M1 if False else Timeframe.D1,
                                 time=ts, open=100+i, high=100+i, low=100+i, close=100+i, volume=1e6))
            rows.append(OhlcvBar(asset_id=b.id, timeframe=Timeframe.D1,
                                 time=ts, open=300-i, high=300-i, low=300-i, close=300-i, volume=1e6))
        db.add_all(rows)
        s = Strategy(user_id=u.id, name="pf"); db.add(s); db.flush()
        sv = StrategyVersion(strategy_id=s.id, version=1, code=""); db.add(sv); db.commit()
        ctx = {"uid": u.id, "aid": a.id, "bid": b.id, "sid": s.id, "svid": sv.id}
    yield ctx
    with SessionLocal() as db:
        db.execute(delete(BacktestYearlyResult).where(
            BacktestYearlyResult.backtest_id.in_(
                db.query(Backtest.id).filter_by(user_id=ctx["uid"]).subquery().select())))
        db.execute(delete(Backtest).where(Backtest.user_id == ctx["uid"]))
        db.execute(delete(StrategyVersion).where(StrategyVersion.strategy_id == ctx["sid"]))
        db.execute(delete(Strategy).where(Strategy.id == ctx["sid"]))
        for aid in (ctx["aid"], ctx["bid"]):
            db.execute(delete(OhlcvBar).where(OhlcvBar.asset_id == aid))
            db.execute(delete(Asset).where(Asset.id == aid))
        db.execute(delete(User).where(User.id == ctx["uid"]))
        db.commit()


def test_multi_asset_long_short_runs_and_persists(two_asset_env):
    ctx = two_asset_env
    with SessionLocal() as db:
        bt = Backtest(
            user_id=ctx["uid"], strategy_version_id=ctx["svid"],
            status=BacktestStatus.QUEUED,
            config={
                "asset_ids": [ctx["aid"], ctx["bid"]], "timeframe": "1d",
                "strategy": "cross_sectional_momentum",
                "params": {"lookback": 20, "top_k": 1, "bottom_k": 1},
                "initial_capital": 100000, "commission_bps": 0,
            },
        )
        db.add(bt); db.commit(); db.refresh(bt); btid = bt.id

    run_and_persist(btid)

    with SessionLocal() as db:
        bt = db.get(Backtest, btid)
        assert bt.status == BacktestStatus.COMPLETED
        assert bt.total_return_pct is not None
        # long the up-trending, short the down-trending => positive
        assert bt.total_return_pct > 0
        # diagnostics record the market-neutral construction
        assert bt.diagnostics["n_assets"] == 2
        assert bt.diagnostics["is_market_neutral"] is True
