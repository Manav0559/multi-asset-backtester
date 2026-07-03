"""E6 — consent-based head-to-head competitions.

Three things must hold:
  - State machine: only legal transitions succeed; the rest are 403/409.
  - Consent contract: a participant sees ONLY whitelisted aggregates about the
    other — the opponent's positions/ledger stay behind normal membership authz
    (404), and the comparison payload has a frozen field set (schema snapshot).
  - Immutability: once finished, metrics don't move even as the underlying
    portfolios keep trading.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from app.core.security import create_access_token, hash_password
from app.db.session import SessionLocal
from app.models import (
    Asset, Challenge, LedgerEntry, OhlcvBar, Order, Portfolio, PortfolioMember,
    Position, Trade, User,
)
from app.models.enums import AssetClass, ChallengeStatus, OrderSide, Timeframe
from app.services.challenges import finish_expired
from app.services.execution import execute_market_order

_PW = hash_password("s3cret-pass!")


def _user(tag):
    s = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        u = User(email=f"ch_{tag}_{s}@x.com", username=f"ch_{tag}_{s}", hashed_password=_PW)
        db.add(u); db.commit(); db.refresh(u)
        return {"id": u.id, "username": u.username,
                "h": {"Authorization": f"Bearer {create_access_token(u.id)}"}}


def _portfolio(client, headers, cash="10000.00"):
    return client.post("/portfolios", headers=headers,
                       json={"name": f"ch {uuid.uuid4().hex[:6]}", "initial_cash": cash}).json()["id"]


def _bump_price(aid, close, day):
    with SessionLocal() as db:
        db.add(OhlcvBar(asset_id=aid, timeframe=Timeframe.M1,
                        time=datetime(2025, 6, day, tzinfo=timezone.utc),
                        open=close, high=close, low=close, close=close, volume=1))
        db.commit()


@pytest.fixture()
def ch_env(client):
    alice, bob = _user("a"), _user("b")
    with SessionLocal() as db:
        asset = Asset(symbol=f"CH{uuid.uuid4().hex[:6].upper()}", exchange="TEST",
                      asset_class=AssetClass.CRYPTO)
        db.add(asset); db.commit(); db.refresh(asset)
        aid = asset.id
    _bump_price(aid, 100, 1)
    pa = _portfolio(client, alice["h"])
    pb = _portfolio(client, bob["h"])
    yield {"alice": alice, "bob": bob, "aid": aid, "pa": pa, "pb": pb}
    with SessionLocal() as db:
        for pid in (pa, pb):
            pu = uuid.UUID(pid)
            db.execute(delete(LedgerEntry).where(LedgerEntry.portfolio_id == pu))
            db.execute(delete(Trade).where(Trade.portfolio_id == pu))
            db.execute(delete(Order).where(Order.portfolio_id == pu))
            db.execute(delete(Position).where(Position.portfolio_id == pu))
            db.execute(delete(PortfolioMember).where(PortfolioMember.portfolio_id == pu))
        db.execute(delete(Challenge).where(Challenge.challenger_id.in_([alice["id"], bob["id"]])))
        db.execute(delete(Portfolio).where(Portfolio.id.in_([uuid.UUID(pa), uuid.UUID(pb)])))
        db.execute(delete(OhlcvBar).where(OhlcvBar.asset_id == aid))
        db.execute(delete(Asset).where(Asset.id == aid))
        db.execute(delete(User).where(User.id.in_([alice["id"], bob["id"]])))
        db.commit()


def _create(client, env, days=7):
    return client.post("/challenges", headers=env["alice"]["h"], json={
        "opponent_username": env["bob"]["username"],
        "challenger_portfolio_id": env["pa"], "duration_days": days}).json()


# ------------------------------------------------------------ state machine --
def test_challenge_state_machine(client, ch_env):
    ch = _create(client, ch_env)
    cid = ch["id"]
    assert ch["status"] == "pending" and ch["opponent_username"] == ch_env["bob"]["username"]

    # Wrong actor / wrong state.
    assert client.post(f"/challenges/{cid}/cancel", headers=ch_env["bob"]["h"]).status_code == 403
    assert client.post(f"/challenges/{cid}/accept", headers=ch_env["alice"]["h"],
                       json={"opponent_portfolio_id": ch_env["pb"]}).status_code == 403
    # Non-participant can't even see it.
    carol = _user("c")
    assert client.get(f"/challenges/{cid}", headers=carol["h"]).status_code == 404
    with SessionLocal() as db:
        db.execute(delete(User).where(User.id == carol["id"])); db.commit()

    # Opponent accepts ⇒ active.
    r = client.post(f"/challenges/{cid}/accept", headers=ch_env["bob"]["h"],
                    json={"opponent_portfolio_id": ch_env["pb"]})
    assert r.status_code == 200 and r.json()["status"] == "active"
    assert r.json()["start_at"] and r.json()["end_at"]

    # Illegal now: accept again, decline, cancel active.
    assert client.post(f"/challenges/{cid}/accept", headers=ch_env["bob"]["h"],
                       json={"opponent_portfolio_id": ch_env["pb"]}).status_code == 409
    assert client.post(f"/challenges/{cid}/decline", headers=ch_env["bob"]["h"]).status_code == 409
    assert client.post(f"/challenges/{cid}/cancel", headers=ch_env["alice"]["h"]).status_code == 409


def test_decline_and_cancel(client, ch_env):
    ch = _create(client, ch_env)
    assert client.post(f"/challenges/{ch['id']}/decline",
                       headers=ch_env["bob"]["h"]).json()["status"] == "declined"
    ch2 = _create(client, ch_env)
    assert client.post(f"/challenges/{ch2['id']}/cancel",
                       headers=ch_env["alice"]["h"]).json()["status"] == "cancelled"


# --------------------------------------------------------- consent contract --
_ALLOWED_FIELDS = {"return_pct", "max_drawdown_pct", "sharpe", "win_rate",
                   "n_trades", "equity", "curve"}


def test_consent_contract(client, ch_env):
    ch = _create(client, ch_env)
    cid = ch["id"]
    client.post(f"/challenges/{cid}/accept", headers=ch_env["bob"]["h"],
                json={"opponent_portfolio_id": ch_env["pb"]})

    # Alice can see the comparison...
    r = client.get(f"/challenges/{cid}", headers=ch_env["alice"]["h"])
    assert r.status_code == 200
    body = r.json()
    # ...but ONLY whitelisted aggregate fields about the opponent.
    assert set(body["them"].keys()) == _ALLOWED_FIELDS, "consent contract leaked a field"
    assert set(body["you"].keys()) == _ALLOWED_FIELDS
    assert body["frozen"] is False

    # Alice CANNOT reach bob's raw data — she isn't a member of his portfolio.
    assert client.get(f"/portfolios/{ch_env['pb']}/positions",
                      headers=ch_env["alice"]["h"]).status_code == 404
    assert client.get(f"/portfolios/{ch_env['pb']}/ledger",
                      headers=ch_env["alice"]["h"]).status_code == 404


# --------------------------------------------------------- finish immutable --
def test_finish_freezes_metrics(client, ch_env):
    ch = _create(client, ch_env)
    cid = ch["id"]
    client.post(f"/challenges/{cid}/accept", headers=ch_env["bob"]["h"],
                json={"opponent_portfolio_id": ch_env["pb"]})

    # Alice trades then the market rises 20% ⇒ she's up, bob flat.
    with SessionLocal() as db:
        execute_market_order(db, portfolio_id=uuid.UUID(ch_env["pa"]),
                             user_id=ch_env["alice"]["id"], asset_id=ch_env["aid"],
                             side=OrderSide.BUY, qty=Decimal("10"))
    _bump_price(ch_env["aid"], 120, 2)

    # Force expiry and finish.
    with SessionLocal() as db:
        c = db.get(Challenge, uuid.UUID(cid))
        c.end_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
        assert finish_expired(db) == 1
        c = db.get(Challenge, uuid.UUID(cid))
        assert c.status == ChallengeStatus.FINISHED
        assert c.winner_id == ch_env["alice"]["id"]
        frozen_alice_return = c.final_metrics["challenger"]["return_pct"]
    assert frozen_alice_return == pytest.approx(2.0, abs=0.01)  # +2% (10 sh, 100→120 on 10k)

    r1 = client.get(f"/challenges/{cid}", headers=ch_env["alice"]["h"]).json()
    assert r1["frozen"] is True
    assert r1["you"]["return_pct"] == pytest.approx(2.0, abs=0.01)

    # Portfolios keep trading — market doubles — but the FROZEN result is unchanged.
    _bump_price(ch_env["aid"], 240, 3)
    r2 = client.get(f"/challenges/{cid}", headers=ch_env["alice"]["h"]).json()
    assert r2["you"]["return_pct"] == r1["you"]["return_pct"]  # immutable
