"""Backtest admission control — over-budget jobs are rejected at submit time
(fast, actionable 422), not OOM-killed minutes later in the worker."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete

import app.api.routes.backtests as backtests_route
from app.core.security import create_access_token, hash_password
from app.db.session import SessionLocal
from app.models import Asset, Backtest, OhlcvBar, Strategy, User
from app.models.enums import AssetClass, Timeframe

_PW = hash_password("s3cret-pass!")
N_BARS = 10


@pytest.fixture()
def ac_env(client):
    s = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        u = User(email=f"ac_{s}@x.com", username=f"ac_{s}", hashed_password=_PW)
        db.add(u); db.commit(); db.refresh(u)
        uid = u.id
        asset = Asset(symbol=f"AC{s[:6].upper()}", exchange="BINANCE",
                      asset_class=AssetClass.CRYPTO)
        db.add(asset); db.commit(); db.refresh(asset)
        base = datetime(2025, 6, 1, tzinfo=timezone.utc)
        for i in range(N_BARS):
            db.add(OhlcvBar(asset_id=asset.id, timeframe=Timeframe.D1,
                            time=base + timedelta(days=i),
                            open=100, high=101, low=99, close=100 + i, volume=10))
        db.commit()
        aid = asset.id
    h = {"Authorization": f"Bearer {create_access_token(uid)}"}
    sv = client.post("/strategies", headers=h,
                     json={"name": f"ac {s}", "code": ""}).json()
    yield {"h": h, "aid": aid, "uid": uid, "sv": sv["version_id"]}
    with SessionLocal() as db:
        db.execute(delete(Backtest).where(Backtest.user_id == uid))
        db.execute(delete(Strategy).where(Strategy.user_id == uid))
        db.execute(delete(OhlcvBar).where(OhlcvBar.asset_id == aid))
        db.execute(delete(Asset).where(Asset.id == aid))
        db.execute(delete(User).where(User.id == uid))
        db.commit()


def _payload(env):
    return {"strategy_version_id": env["sv"], "asset_id": env["aid"],
            "timeframe": "1d", "strategy": "sma_crossover",
            "params": {"fast": 2, "slow": 3}}


def test_estimator_counts_only_requested_slice(ac_env):
    from app.schemas.backtest import BacktestCreate
    with SessionLocal() as db:
        est_all = backtests_route._estimate_working_set_mb(
            db, BacktestCreate(**_payload(ac_env)))
        est_none = backtests_route._estimate_working_set_mb(
            db, BacktestCreate(**{**_payload(ac_env), "end": "2020-01-01"}))
    # Formula on N_BARS is well under 1MB (integer MB -> 0); an empty window
    # must estimate exactly 0. The point: the COUNT respects the requested slice.
    assert est_all >= est_none == 0


def test_over_budget_job_rejected_422_with_actionable_detail(client, ac_env, monkeypatch):
    # Blow up the multiplier so the REAL path (COUNT -> estimate -> compare)
    # crosses the budget with only N_BARS rows.
    monkeypatch.setattr(backtests_route, "_WORKING_SET_MULTIPLIER", 2**32)
    r = client.post("/backtests", headers=ac_env["h"], json=_payload(ac_env))
    assert r.status_code == 422
    assert "working set" in r.json()["detail"]
    assert "job budget" in r.json()["detail"]
    # actionable: tells the user what to change
    assert "timeframe" in r.json()["detail"]
    # nothing was queued
    with SessionLocal() as db:
        assert db.query(Backtest).filter_by(user_id=ac_env["uid"]).count() == 0


def test_under_budget_job_still_accepted(client, ac_env):
    r = client.post("/backtests", headers=ac_env["h"], json=_payload(ac_env))
    assert r.status_code == 202
    assert r.json()["status"] == "queued"
