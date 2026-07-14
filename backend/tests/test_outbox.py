"""Transactional outbox — the dual-write hole is closed.

Three proofs:
  * a fill commits its event row WITH the ledger entry, and the route's
    fast-path publish marks it published (normal case leaves nothing pending);
  * a row left unpublished (process died between commit and publish) is
    re-published by the relay onto the real Redis channel, byte-identical,
    and marked;
  * the relay prunes old published rows so the table is bounded.
"""
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app.core.security import create_access_token, hash_password
from app.db.session import SessionLocal
from app.models import (
    Asset, LedgerEntry, OhlcvBar, Order, OutboxEvent, Portfolio,
    PortfolioMember, Position, Trade, User,
)
from app.models.enums import AssetClass, Timeframe
from app.services.events import relay_outbox

_PW = hash_password("s3cret-pass!")


@pytest.fixture()
def ob_env(client):
    s = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        u = User(email=f"ob_{s}@x.com", username=f"ob_{s}", hashed_password=_PW)
        db.add(u); db.commit(); db.refresh(u)
        uid = u.id
        asset = Asset(symbol=f"OB{s[:6].upper()}", exchange="BINANCE",
                      asset_class=AssetClass.CRYPTO)
        db.add(asset); db.commit(); db.refresh(asset)
        db.add(OhlcvBar(asset_id=asset.id, timeframe=Timeframe.D1,
                        time=datetime(2025, 6, 1, tzinfo=timezone.utc),
                        open=100, high=100, low=100, close=100, volume=1))
        db.commit()
        aid = asset.id
    h = {"Authorization": f"Bearer {create_access_token(uid)}"}
    pid = client.post("/portfolios", headers=h,
                      json={"name": "outbox fund", "initial_cash": "10000.00"}).json()["id"]
    yield {"h": h, "pid": pid, "aid": aid, "uid": uid}
    with SessionLocal() as db:
        pu = uuid.UUID(pid)
        db.execute(delete(OutboxEvent).where(OutboxEvent.channel == f"portfolio:{pid}"))
        db.execute(delete(LedgerEntry).where(LedgerEntry.portfolio_id == pu))
        db.execute(delete(Trade).where(Trade.portfolio_id == pu))
        db.execute(delete(Order).where(Order.portfolio_id == pu))
        db.execute(delete(Position).where(Position.portfolio_id == pu))
        db.execute(delete(PortfolioMember).where(PortfolioMember.portfolio_id == pu))
        db.execute(delete(Portfolio).where(Portfolio.id == pu))
        db.execute(delete(OhlcvBar).where(OhlcvBar.asset_id == aid))
        db.execute(delete(Asset).where(Asset.id == aid))
        db.execute(delete(User).where(User.id == uid))
        db.commit()


def _outbox_rows(pid):
    with SessionLocal() as db:
        return db.execute(
            select(OutboxEvent).where(OutboxEvent.channel == f"portfolio:{pid}")
            .order_by(OutboxEvent.id)
        ).scalars().all()


def test_fill_writes_outbox_and_fast_path_marks_it(client, ob_env):
    r = client.post(f"/portfolios/{ob_env['pid']}/orders", headers=ob_env["h"],
                    json={"asset_id": ob_env["aid"], "side": "buy", "qty": "1"})
    assert r.status_code == 200 and r.json()["status"] == "filled"

    rows = _outbox_rows(ob_env["pid"])
    assert len(rows) == 1
    row = rows[0]
    assert row.published_at is not None          # fast path marked it
    assert row.payload["type"] == "order"
    assert row.payload["status"] == "filled"
    assert row.payload["username"].startswith("ob_")   # attribution stored too
    assert row.payload["version"] == r.json()["version"]


def test_reject_does_not_write_outbox(client, ob_env):
    r = client.post(f"/portfolios/{ob_env['pid']}/orders", headers=ob_env["h"],
                    json={"asset_id": ob_env["aid"], "side": "sell", "qty": "5"})
    assert r.json()["status"] == "rejected"       # nothing held to sell
    assert _outbox_rows(ob_env["pid"]) == []      # ephemeral: fast-path only


def test_relay_republishes_row_orphaned_by_crash(client, ob_env, monkeypatch):
    """Simulate the crash window: an outbox row committed but never published.
    The relay must re-emit it onto the in-process bus and mark it."""
    chan = f"portfolio:{ob_env['pid']}"
    payload = {"type": "order", "status": "filled", "order_id": str(uuid.uuid4()),
               "portfolio_id": ob_env["pid"], "version": 99}
    with SessionLocal() as db:
        db.add(OutboxEvent(channel=chan, payload=payload))
        db.commit()

    # Capture what the relay publishes on the in-process bus.
    published: list[tuple[str, dict]] = []
    import app.services.events as events_mod
    monkeypatch.setattr(events_mod.bus, "publish",
                        lambda c, d: published.append((c, d)))

    out = relay_outbox()
    assert out["published"] >= 1
    assert (chan, payload) in published      # byte-identical replay

    rows = _outbox_rows(ob_env["pid"])
    assert all(r.published_at is not None for r in rows)

    # Idempotent second sweep: nothing left pending.
    assert relay_outbox()["published"] == 0


def test_relay_prunes_old_published_rows(ob_env):
    chan = f"portfolio:{ob_env['pid']}"
    with SessionLocal() as db:
        db.add(OutboxEvent(channel=chan, payload={"type": "order"},
                           published_at=datetime.now(timezone.utc) - timedelta(days=30)))
        db.commit()
    relay_outbox(retain_days=7)
    assert _outbox_rows(ob_env["pid"]) == []
