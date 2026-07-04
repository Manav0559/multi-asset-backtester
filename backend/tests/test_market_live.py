"""Live-market surfaces: status (open/closed), snapshot (last tick/depth),
and last-session volume profile — with honest provenance badges."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete

from app.db.session import SessionLocal
from app.models import Asset, OhlcvBar
from app.models.enums import AssetClass, Timeframe
from app.services.market_hours import market_status


def test_crypto_is_always_open_live():
    s = market_status("BINANCE", AssetClass.CRYPTO)
    assert s["is_open"] is True and s["provenance"] == "live"


def test_equity_status_has_calendar_provenance():
    s = market_status("NASDAQ", AssetClass.US_EQUITY)
    # is_open is True/False (never None for a known exchange); provenance is
    # delayed while open, last_session while closed — never "live".
    assert s["is_open"] in (True, False)
    assert s["provenance"] in ("delayed", "last_session")


def test_unknown_exchange_degrades_not_lies():
    s = market_status("MARS", AssetClass.US_EQUITY)
    assert s["is_open"] is None  # unknown, honest


@pytest.fixture()
def vp_env(client):
    from app.core.security import create_access_token, hash_password
    from app.models import User
    s = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        u = User(email=f"vp_{s}@x.com", username=f"vp_{s}", hashed_password=hash_password("x"))
        db.add(u); db.commit(); db.refresh(u)
        uid = u.id
        asset = Asset(symbol=f"VP{s[:5].upper()}", exchange="NASDAQ",
                      asset_class=AssetClass.US_EQUITY)
        db.add(asset); db.commit(); db.refresh(asset)
        aid = asset.id
        base = datetime(2025, 6, 2, 13, 30, tzinfo=timezone.utc)
        for i in range(60):
            px = 100 + (i % 10)
            db.add(OhlcvBar(asset_id=aid, timeframe=Timeframe.M1,
                            time=base + timedelta(minutes=i),
                            open=px, high=px + 1, low=px - 1, close=px, volume=1000 + i))
        db.commit()
    headers = {"Authorization": f"Bearer {create_access_token(uid)}"}
    yield {"aid": aid, "headers": headers}
    with SessionLocal() as db:
        db.execute(delete(OhlcvBar).where(OhlcvBar.asset_id == aid))
        db.execute(delete(Asset).where(Asset.id == aid))
        db.execute(delete(User).where(User.id == uid))
        db.commit()


def test_volume_profile_last_session(client, vp_env):
    r = client.get(f"/market/{vp_env['aid']}/volume-profile", headers=vp_env["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provenance"] == "last_session"
    assert body["session_date"]
    assert len(body["levels"]) >= 3
    # Levels are price-sorted descending and carry positive volume.
    prices = [lvl["price"] for lvl in body["levels"]]
    assert prices == sorted(prices, reverse=True)
    assert all(lvl["volume"] > 0 for lvl in body["levels"])


def test_snapshot_empty_when_no_feed(client, vp_env):
    r = client.get(f"/market/{vp_env['aid']}/snapshot", headers=vp_env["headers"])
    assert r.status_code == 200
    body = r.json()
    # No live feed for this synthetic equity → tick/depth are None but the
    # channels + status are still described.
    assert body["tick"] is None
    assert body["provenance"] in ("delayed", "last_session")
    assert body["channels"]["tick"].startswith("tick:")
