"""E5a — membership authorization matrix + invite accept/decline lifecycle.

Every portfolio route flows through require_portfolio_role. The matrix proves
the three roles get exactly the access they should across five actions:

              read  trade  invite  rename  delete
  owner        ✓     ✓      ✓       ✓       ✓
  trader       ✓     ✓      ✗(403)  ✗(403)  ✗(403)
  viewer       ✓     ✗(403) ✗(403)  ✗(403)  ✗(403)

A non-member gets 404 (not 403) so portfolio IDs can't be probed.
"""
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select

from app.core.security import create_access_token, hash_password
from app.db.session import SessionLocal
from app.models import (
    Asset, LedgerEntry, OhlcvBar, Order, Portfolio, PortfolioInvite,
    PortfolioMember, Position, Trade, User,
)
from app.models.enums import AssetClass, PortfolioRole, Timeframe

_PW_HASH = hash_password("s3cret-pass!")  # hash once; identical across test users


def _make_user(tag: str) -> dict:
    """Create a user directly in the DB and mint an access token — no HTTP,
    no per-user bcrypt (the shared hash is reused)."""
    s = uuid.uuid4().hex[:10]
    email = f"az_{tag}_{s}@example.com"
    with SessionLocal() as db:
        u = User(email=email, username=f"az_{tag}_{s}", hashed_password=_PW_HASH)
        db.add(u); db.commit(); db.refresh(u)
        uid = u.id
    return {"h": {"Authorization": f"Bearer {create_access_token(uid)}"}, "email": email}


@pytest.fixture(scope="module")
def az_users():
    """4 users + 1 asset, created ONCE."""
    users = {k: _make_user(k) for k in ("owner", "trader", "viewer", "outsider")}
    with SessionLocal() as db:
        asset = Asset(symbol=f"AZ{uuid.uuid4().hex[:6].upper()}", exchange="TEST",
                      asset_class=AssetClass.CRYPTO)
        db.add(asset); db.commit(); db.refresh(asset)
        db.add(OhlcvBar(asset_id=asset.id, timeframe=Timeframe.M1,
                        time=datetime(2025, 6, 1, tzinfo=timezone.utc),
                        open=100, high=100, low=100, close=100, volume=1))
        db.commit()
        aid = asset.id
    yield {"users": users, "aid": aid}
    with SessionLocal() as db:
        db.execute(delete(OhlcvBar).where(OhlcvBar.asset_id == aid))
        db.execute(delete(Asset).where(Asset.id == aid))
        db.execute(delete(User).where(User.email.in_([u["email"] for u in users.values()])))
        db.commit()


@pytest.fixture()
def az_env(client, az_users):
    """A FRESH portfolio per test (matrix cases trade/rename/delete it), reusing
    the module-scoped users + asset."""
    users, aid = az_users["users"], az_users["aid"]
    owner, trader, viewer, outsider = (users["owner"], users["trader"],
                                       users["viewer"], users["outsider"])
    pid = client.post("/portfolios", headers=owner["h"],
                      json={"name": "authz fund", "initial_cash": "10000.00"}).json()["id"]
    with SessionLocal() as db:
        for who, role in ((trader, PortfolioRole.TRADER), (viewer, PortfolioRole.VIEWER)):
            uid = db.scalar(select(User.id).where(User.email == who["email"]))
            db.add(PortfolioMember(portfolio_id=uuid.UUID(pid), user_id=uid, role=role))
        db.commit()
    yield {"owner": owner, "trader": trader, "viewer": viewer, "outsider": outsider,
           "pid": pid, "aid": aid}
    with SessionLocal() as db:
        pu = uuid.UUID(pid)
        db.execute(delete(LedgerEntry).where(LedgerEntry.portfolio_id == pu))
        db.execute(delete(Trade).where(Trade.portfolio_id == pu))
        db.execute(delete(Order).where(Order.portfolio_id == pu))
        db.execute(delete(Position).where(Position.portfolio_id == pu))
        db.execute(delete(PortfolioInvite).where(PortfolioInvite.portfolio_id == pu))
        db.execute(delete(PortfolioMember).where(PortfolioMember.portfolio_id == pu))
        db.execute(delete(Portfolio).where(Portfolio.id == pu))
        db.commit()


def _act(client, action, env, headers):
    pid, aid = env["pid"], env["aid"]
    if action == "read":
        return client.get(f"/portfolios/{pid}", headers=headers).status_code
    if action == "trade":
        return client.post(f"/portfolios/{pid}/orders", headers=headers,
                           json={"asset_id": aid, "side": "buy", "qty": "1"}).status_code
    if action == "invite":
        return client.post(f"/portfolios/{pid}/invites", headers=headers,
                           json={"invitee_email": "x@y.com", "role": "trader"}).status_code
    if action == "rename":
        return client.patch(f"/portfolios/{pid}", headers=headers,
                            json={"name": "renamed"}).status_code
    if action == "delete":
        return client.delete(f"/portfolios/{pid}", headers=headers).status_code
    raise ValueError(action)


@pytest.mark.parametrize("role,action,forbidden", [
    ("trader", "read", False), ("trader", "trade", False),
    ("trader", "invite", True), ("trader", "rename", True), ("trader", "delete", True),
    ("viewer", "read", False), ("viewer", "trade", True),
    ("viewer", "invite", True), ("viewer", "rename", True), ("viewer", "delete", True),
    ("owner", "read", False), ("owner", "trade", False), ("owner", "invite", False),
    ("owner", "rename", False), ("owner", "delete", False),
])
def test_authz_matrix(client, az_env, role, action, forbidden):
    code = _act(client, action, az_env, az_env[role]["h"])
    if forbidden:
        assert code == 403, f"{role}/{action} expected 403, got {code}"
    else:
        assert code != 403, f"{role}/{action} unexpectedly 403"


def test_non_member_gets_404_not_403(client, az_env):
    # Outsider can't even tell the portfolio exists.
    code = client.get(f"/portfolios/{az_env['pid']}", headers=az_env["outsider"]["h"]).status_code
    assert code == 404


def test_invite_accept_and_decline(client, az_env):
    owner_h = az_env["owner"]["h"]
    newcomer = _make_user("new")
    # Owner invites the newcomer's email.
    inv = client.post(f"/portfolios/{az_env['pid']}/invites", headers=owner_h,
                      json={"invitee_email": newcomer["email"], "role": "trader"}).json()
    # Newcomer sees it pending.
    pending = client.get("/portfolios/invites/pending", headers=newcomer["h"]).json()
    assert any(p["token"] == inv["token"] for p in pending)
    # Accept ⇒ becomes a member; the invite leaves pending.
    r = client.post("/portfolios/invites/accept", headers=newcomer["h"],
                    json={"token": inv["token"]})
    assert r.status_code == 200 and r.json()["role"] == "trader"
    assert client.get("/portfolios/invites/pending", headers=newcomer["h"]).json() == []

    # A second invite can be declined.
    inv2 = client.post(f"/portfolios/{az_env['pid']}/invites", headers=owner_h,
                       json={"invitee_email": newcomer["email"], "role": "viewer"}).json()
    assert client.post("/portfolios/invites/decline", headers=newcomer["h"],
                       json={"token": inv2["token"]}).status_code == 204
    # Declined invite is gone from pending; role unchanged (still trader).
    assert client.get("/portfolios/invites/pending", headers=newcomer["h"]).json() == []
    with SessionLocal() as db:
        db.execute(delete(User).where(User.email == newcomer["email"]))
        db.commit()
