"""Delete/leave/name endpoints added in the UX pass."""
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select

from app.core.security import create_access_token, hash_password
from app.db.session import SessionLocal
from app.models import (
    Asset, Backtest, OhlcvBar, PortfolioMember, Strategy, StrategyVersion, User,
)
from app.models.enums import AssetClass, PortfolioRole, Timeframe

_PW = hash_password("s3cret-pass!")


def _user(tag):
    s = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        u = User(email=f"{tag}_{s}@x.com", username=f"{tag}_{s}", hashed_password=_PW)
        db.add(u); db.commit(); db.refresh(u)
        return {"id": u.id, "h": {"Authorization": f"Bearer {create_access_token(u.id)}"}}


@pytest.fixture()
def crud_env(client):
    owner, member, outsider = _user("own"), _user("mem"), _user("out")
    pid = client.post("/portfolios", headers=owner["h"],
                      json={"name": "crud fund", "initial_cash": "1000.00"}).json()["id"]
    with SessionLocal() as db:
        db.add(PortfolioMember(portfolio_id=uuid.UUID(pid), user_id=member["id"],
                               role=PortfolioRole.TRADER))
        db.commit()
    yield {"owner": owner, "member": member, "outsider": outsider, "pid": pid}
    with SessionLocal() as db:
        from app.models import LedgerEntry, Portfolio
        pu = uuid.UUID(pid)
        db.execute(delete(LedgerEntry).where(LedgerEntry.portfolio_id == pu))
        db.execute(delete(PortfolioMember).where(PortfolioMember.portfolio_id == pu))
        db.execute(delete(Portfolio).where(Portfolio.id == pu))  # may already be gone
        db.execute(delete(User).where(User.id.in_(
            [owner["id"], member["id"], outsider["id"]])))
        db.commit()


def test_member_can_leave_owner_cannot(client, crud_env):
    pid = crud_env["pid"]
    # outsider is not a member -> 404 (probe-proof)
    assert client.post(f"/portfolios/{pid}/leave",
                       headers=crud_env["outsider"]["h"]).status_code == 404
    # owner cannot leave -> 409
    assert client.post(f"/portfolios/{pid}/leave",
                       headers=crud_env["owner"]["h"]).status_code == 409
    # member leaves -> 204, then loses access -> 404
    assert client.post(f"/portfolios/{pid}/leave",
                       headers=crud_env["member"]["h"]).status_code == 204
    assert client.get(f"/portfolios/{pid}",
                      headers=crud_env["member"]["h"]).status_code == 404
    # portfolio still exists for the owner
    assert client.get(f"/portfolios/{pid}", headers=crud_env["owner"]["h"]).status_code == 200


def test_owner_deletes_portfolio(client, crud_env):
    pid = crud_env["pid"]
    assert client.delete(f"/portfolios/{pid}",
                         headers=crud_env["member"]["h"]).status_code == 403  # trader can't
    assert client.delete(f"/portfolios/{pid}",
                         headers=crud_env["owner"]["h"]).status_code == 204
    assert client.get(f"/portfolios/{pid}", headers=crud_env["owner"]["h"]).status_code == 404


def test_backtest_name_and_delete(client, crud_env):
    owner = crud_env["owner"]
    with SessionLocal() as db:
        a = Asset(symbol=f"BT{uuid.uuid4().hex[:5].upper()}", exchange="NASDAQ",
                  asset_class=AssetClass.US_EQUITY, currency="USD")
        db.add(a); db.commit(); db.refresh(a)
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        from datetime import timedelta
        db.add_all([OhlcvBar(asset_id=a.id, timeframe=Timeframe.D1,
                             time=base + timedelta(days=i), open=100 + i, high=101 + i,
                             low=99 + i, close=100 + i, volume=1e6) for i in range(120)])
        db.commit(); aid = a.id
    sv = client.post("/strategies", headers=owner["h"],
                     json={"name": f"s{uuid.uuid4().hex[:6]}", "code": ""}).json()
    r = client.post("/backtests", headers=owner["h"], json={
        "strategy_version_id": sv["version_id"], "asset_id": aid, "timeframe": "1d",
        "strategy": "sma_crossover", "params": {"fast": 5, "slow": 20},
        "name": "My Golden Cross"}).json()
    bid = r["id"]
    assert r["config"]["label"] == "My Golden Cross"        # name stored as label
    # outsider can't delete -> 404 (probe-proof)
    assert client.delete(f"/backtests/{bid}",
                         headers=crud_env["outsider"]["h"]).status_code == 404
    assert client.delete(f"/backtests/{bid}", headers=owner["h"]).status_code == 204
    assert client.get(f"/backtests/{bid}", headers=owner["h"]).status_code == 404
    with SessionLocal() as db:
        db.execute(delete(OhlcvBar).where(OhlcvBar.asset_id == aid))
        db.execute(delete(Asset).where(Asset.id == aid))
        db.execute(delete(StrategyVersion).where(
            StrategyVersion.strategy_id.in_(select(Strategy.id).where(Strategy.user_id == owner["id"]))))
        db.execute(delete(Strategy).where(Strategy.user_id == owner["id"]))
        db.commit()


def test_leaderboard_route_is_gone(client, crud_env):
    assert client.get("/leaderboard", headers=crud_env["owner"]["h"]).status_code == 404
