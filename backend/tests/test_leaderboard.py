"""Leaderboard + equity-history tests.

The headline is test_two_portfolio_ranking: two public portfolios, one trades
into an asset whose price then rises 20% — it must outrank the idle one with
an exact Decimal return computed from equity = cash + marked positions.
Assertions filter to the portfolios created here, so leftover public
portfolios in a dev database can't break the suite.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from app.db.session import SessionLocal
from app.models import (
    Asset,
    LedgerEntry,
    OhlcvBar,
    Order,
    Portfolio,
    PortfolioEquitySnapshot,
    PortfolioMember,
    Position,
    Trade,
    User,
)
from app.models.enums import AssetClass, Timeframe

INITIAL = "1000.00"


def _register(client, tag: str) -> dict:
    """Register + login a throwaway user; returns {'headers', 'username', 'email'}."""
    suffix = uuid.uuid4().hex[:10]
    email = f"lb_{tag}_{suffix}@example.com"
    username = f"lb_{tag}_{suffix}"
    password = "s3cret-pass!"
    r = client.post("/auth/register",
                    json={"email": email, "username": username, "password": password})
    assert r.status_code == 201, r.text
    r = client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    return {"headers": {"Authorization": f"Bearer {token}"},
            "username": username, "email": email}


@pytest.fixture()
def lb_env(client):
    """Two users, an asset priced at 100, and full teardown (portfolios are
    created inside the tests; teardown sweeps everything owned by the users)."""
    alice = _register(client, "alice")
    bob = _register(client, "bob")
    with SessionLocal() as db:
        asset = Asset(symbol=f"LB{uuid.uuid4().hex[:6].upper()}", exchange="TEST",
                      asset_class=AssetClass.CRYPTO)
        db.add(asset); db.commit(); db.refresh(asset)
        db.add(OhlcvBar(asset_id=asset.id, timeframe=Timeframe.M1,
                        time=datetime(2025, 6, 1, tzinfo=timezone.utc),
                        open=100, high=100, low=100, close=100, volume=1))
        db.commit()
        asset_id = asset.id
    yield {"alice": alice, "bob": bob, "asset_id": asset_id}
    with SessionLocal() as db:
        emails = [alice["email"], bob["email"]]
        user_ids = db.scalars(select(User.id).where(User.email.in_(emails))).all()
        pids = db.scalars(select(Portfolio.id).where(Portfolio.owner_id.in_(user_ids))).all()
        db.execute(delete(PortfolioEquitySnapshot)
                   .where(PortfolioEquitySnapshot.portfolio_id.in_(pids)))
        db.execute(delete(LedgerEntry).where(LedgerEntry.portfolio_id.in_(pids)))
        db.execute(delete(Trade).where(Trade.portfolio_id.in_(pids)))
        db.execute(delete(Order).where(Order.portfolio_id.in_(pids)))
        db.execute(delete(Position).where(Position.portfolio_id.in_(pids)))
        db.execute(delete(PortfolioMember).where(PortfolioMember.portfolio_id.in_(pids)))
        db.execute(delete(Portfolio).where(Portfolio.id.in_(pids)))
        db.execute(delete(OhlcvBar).where(OhlcvBar.asset_id == asset_id))
        db.execute(delete(Asset).where(Asset.id == asset_id))
        db.execute(delete(User).where(User.id.in_(user_ids)))
        db.commit()


def _create_portfolio(client, headers, name: str, is_public: bool = True) -> str:
    r = client.post("/portfolios", headers=headers,
                    json={"name": name, "initial_cash": INITIAL, "is_public": is_public})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _bump_price(asset_id: int, close: int, day: int) -> None:
    with SessionLocal() as db:
        db.add(OhlcvBar(asset_id=asset_id, timeframe=Timeframe.M1,
                        time=datetime(2025, 6, day, tzinfo=timezone.utc),
                        open=close, high=close, low=close, close=close, volume=1))
        db.commit()


# ------------------------------------------------------------- leaderboard --
def test_two_portfolio_ranking(client, lb_env):
    alice, bob, asset_id = lb_env["alice"], lb_env["bob"], lb_env["asset_id"]
    pid_a = _create_portfolio(client, alice["headers"], "alpha fund")
    pid_b = _create_portfolio(client, bob["headers"], "idle fund")

    # Alice buys 5 @ 100 (cash 500, position 5), then the market rises to 120:
    # equity = 500 + 5*120 = 1100 -> +10%. Bob stays in cash -> 0%.
    r = client.post(f"/portfolios/{pid_a}/orders", headers=alice["headers"],
                    json={"asset_id": asset_id, "side": "buy", "qty": "5"})
    assert r.status_code == 200 and r.json()["status"] == "filled", r.text
    _bump_price(asset_id, 120, day=2)

    r = client.get("/leaderboard?limit=100", headers=alice["headers"])
    assert r.status_code == 200, r.text
    by_id = {e["portfolio_id"]: e for e in r.json()}
    assert pid_a in by_id and pid_b in by_id

    a, b = by_id[pid_a], by_id[pid_b]
    assert Decimal(a["equity"]) == Decimal("1100.00")
    assert Decimal(a["return_pct"]) == Decimal("10.0000")
    assert Decimal(b["equity"]) == Decimal("1000.00")
    assert Decimal(b["return_pct"]) == Decimal("0.0000")
    # The winner strictly outranks the idle portfolio (global ranks may be
    # offset by other public portfolios in a shared dev DB).
    assert a["rank"] < b["rank"]
    assert alice["username"] in a["members"]
    assert bob["username"] in b["members"]
    # Sparkline ends at current equity.
    assert Decimal(a["spark"][-1]) == Decimal("1100.00")


def test_private_portfolio_hidden(client, lb_env):
    alice = lb_env["alice"]
    pid = _create_portfolio(client, alice["headers"], "secret fund", is_public=False)
    r = client.get("/leaderboard?limit=100", headers=alice["headers"])
    assert r.status_code == 200
    assert pid not in {e["portfolio_id"] for e in r.json()}


def test_leaderboard_requires_auth(client):
    assert client.get("/leaderboard").status_code == 401


# --------------------------------------------------------- windowed ranking --
def _insert_snapshot(pid: str, equity: str, hours_ago: float) -> None:
    from datetime import timedelta
    with SessionLocal() as db:
        db.add(PortfolioEquitySnapshot(
            portfolio_id=uuid.UUID(pid),
            time=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
            cash=Decimal(equity), equity=Decimal(equity)))
        db.commit()


def test_windowed_ranking_diverges_from_all_time(client, lb_env):
    """All-time and 24h boards must disagree when recent momentum opposes
    lifetime performance: A is up 10% overall but DOWN vs its 25h-ago snapshot,
    B is flat overall but UP vs its 25h-ago snapshot."""
    alice, bob, asset_id = lb_env["alice"], lb_env["bob"], lb_env["asset_id"]
    pid_a = _create_portfolio(client, alice["headers"], "old money")
    pid_b = _create_portfolio(client, bob["headers"], "hot hand")

    r = client.post(f"/portfolios/{pid_a}/orders", headers=alice["headers"],
                    json={"asset_id": asset_id, "side": "buy", "qty": "5"})
    assert r.json()["status"] == "filled"
    _bump_price(asset_id, 120, day=2)  # A: 500 + 5*120 = 1100 (+10%), B: 1000 (0%)

    # Snapshots 25h ago (outside the 24h window, so they ARE the baselines):
    # A was at 1200 -> 24h return (1100-1200)/1200 = -8.33%
    # B was at  900 -> 24h return (1000-900)/900  = +11.11%
    _insert_snapshot(pid_a, "1200.00", hours_ago=25)
    _insert_snapshot(pid_b, "900.00", hours_ago=25)

    r = client.get("/leaderboard?limit=100&window=all", headers=alice["headers"])
    by_id = {e["portfolio_id"]: e for e in r.json()}
    assert Decimal(by_id[pid_a]["return_pct"]) == Decimal("10.0000")
    assert Decimal(by_id[pid_b]["return_pct"]) == Decimal("0.0000")
    assert by_id[pid_a]["rank"] < by_id[pid_b]["rank"]

    r = client.get("/leaderboard?limit=100&window=24h", headers=alice["headers"])
    by_id = {e["portfolio_id"]: e for e in r.json()}
    assert Decimal(by_id[pid_a]["return_pct"]) == Decimal("-8.3333")
    assert Decimal(by_id[pid_b]["return_pct"]) == Decimal("11.1111")
    assert by_id[pid_b]["rank"] < by_id[pid_a]["rank"]  # the flip

    # 7d: no snapshot is old enough, so baselines fall back to initial_cash
    # and the board matches all-time.
    r = client.get("/leaderboard?limit=100&window=7d", headers=alice["headers"])
    by_id = {e["portfolio_id"]: e for e in r.json()}
    assert Decimal(by_id[pid_a]["return_pct"]) == Decimal("10.0000")
    assert by_id[pid_a]["rank"] < by_id[pid_b]["rank"]


def test_snapshot_task_marks_positions(client, lb_env):
    """The beat task's service writes cash + qty*latest_close for every
    portfolio in one pass."""
    from app.services.snapshots import snapshot_portfolio_equity

    alice, asset_id = lb_env["alice"], lb_env["asset_id"]
    pid = _create_portfolio(client, alice["headers"], "snapped fund")
    r = client.post(f"/portfolios/{pid}/orders", headers=alice["headers"],
                    json={"asset_id": asset_id, "side": "buy", "qty": "5"})
    assert r.json()["status"] == "filled"
    _bump_price(asset_id, 120, day=2)

    with SessionLocal() as db:
        n = snapshot_portfolio_equity(db)
        assert n >= 1
        snap = db.execute(
            select(PortfolioEquitySnapshot)
            .where(PortfolioEquitySnapshot.portfolio_id == uuid.UUID(pid))
            .order_by(PortfolioEquitySnapshot.time.desc())
        ).scalars().first()
    assert snap is not None
    assert snap.equity == Decimal("1100.00")  # 500 cash + 5 * 120
    assert snap.cash == Decimal("500.00")


def test_leaderboard_rejects_bad_window(client, lb_env):
    r = client.get("/leaderboard?window=1h", headers=lb_env["alice"]["headers"])
    assert r.status_code == 422


# ---------------------------------------------------------- equity history --
def test_equity_history_replays_ledger(client, lb_env):
    alice, asset_id = lb_env["alice"], lb_env["asset_id"]
    pid = _create_portfolio(client, alice["headers"], "curve fund")

    r = client.post(f"/portfolios/{pid}/orders", headers=alice["headers"],
                    json={"asset_id": asset_id, "side": "buy", "qty": "5"})
    assert r.json()["status"] == "filled"
    _bump_price(asset_id, 120, day=2)

    r = client.get(f"/portfolios/{pid}/equity-history", headers=alice["headers"])
    assert r.status_code == 200, r.text
    points = r.json()
    # deposit, buy (equity unchanged at fill), and the marked-to-market "now".
    assert len(points) == 3
    assert Decimal(points[0]["equity"]) == Decimal("1000.00")
    assert Decimal(points[0]["cash"]) == Decimal("1000.00")
    assert Decimal(points[1]["equity"]) == Decimal("1000.00")  # 500 cash + 5*100
    assert Decimal(points[1]["cash"]) == Decimal("500.00")
    assert Decimal(points[2]["equity"]) == Decimal("1100.00")  # 500 cash + 5*120
    assert points[0]["time"] <= points[1]["time"] <= points[2]["time"]


def test_equity_history_non_member_404(client, lb_env):
    alice, bob = lb_env["alice"], lb_env["bob"]
    pid = _create_portfolio(client, alice["headers"], "members only")
    r = client.get(f"/portfolios/{pid}/equity-history", headers=bob["headers"])
    assert r.status_code == 404
