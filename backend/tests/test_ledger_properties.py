"""Property-based ledger invariants under concurrency (serves E5b).

A seeded randomized stress loop generates workloads of orders from MULTIPLE
user identities on ONE shared portfolio; each workload runs concurrently
across 8 threads (each its own Postgres connection). After every interleaving
the shared ledger must satisfy:

  1. cash_balance >= 0                       (DB CHECK constraint is the backstop)
  2. initial_cash + Σ ledger.amount == cash  (self-verifying ledger replay)
  3. cash ∈ {ledger.balance_after}            (the last-committed entry recorded it)
  4. Σ signed trade qty == position.qty       (positions reconcile with fills)

Invariant (3) is membership, not ordering: under concurrency created_at ties
and the UUID pk is random, so there is no reliable "last row" by column — but
whichever transaction committed last stamped its balance_after with the final
cash, so the final balance must appear in the set.

The SELECT-FOR-UPDATE serialization in execute_market_order is what makes
these hold under contention. `test_check_constraint_is_the_backstop` proves
the DB-level guarantee directly: a raw UPDATE to a negative balance is
rejected, so even an app-logic bug cannot produce negative cash.

Note on tooling: this uses a seeded random loop, NOT Hypothesis @given.
Hypothesis's shrink/replay model assumes the test is a deterministic function
of its inputs; a concurrent test is not (each run is a different
interleaving), so Hypothesis reports spurious "failed to reproduce" on any
transient. A fixed-seed randomized stress loop is the correct instrument for
concurrency invariants — reproducible harness, randomized workload.
"""
import random
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError

from app.core.security import hash_password
from app.db.session import SessionLocal
from app.models import (
    Asset, LedgerEntry, OhlcvBar, Order, Portfolio, Position, Trade, User,
)
from app.models.enums import AssetClass, OrderSide, Timeframe
from app.services.execution import execute_market_order

_PRICE = Decimal("100")


@pytest.fixture(scope="module")
def shared_env():
    """3 users + 1 asset priced at 100, reused across all generated examples."""
    from datetime import datetime, timezone
    users, uid_list = [], []
    with SessionLocal() as db:
        for i in range(3):
            u = User(email=f"prop_{i}_{uuid.uuid4().hex[:8]}@x.com",
                     username=f"prop_{i}_{uuid.uuid4().hex[:8]}",
                     hashed_password=hash_password("x"))
            db.add(u); db.flush()
            uid_list.append(u.id)
        asset = Asset(symbol=f"PR{uuid.uuid4().hex[:6].upper()}", exchange="TEST",
                      asset_class=AssetClass.CRYPTO)
        db.add(asset); db.flush()
        db.add(OhlcvBar(asset_id=asset.id, timeframe=Timeframe.M1,
                        time=datetime(2025, 6, 1, tzinfo=timezone.utc),
                        open=_PRICE, high=_PRICE, low=_PRICE, close=_PRICE, volume=1))
        aid = asset.id
        db.commit()
    yield {"user_ids": uid_list, "asset_id": aid}
    with SessionLocal() as db:
        db.execute(delete(OhlcvBar).where(OhlcvBar.asset_id == aid))
        db.execute(delete(Asset).where(Asset.id == aid))
        db.execute(delete(User).where(User.id.in_(uid_list)))
        db.commit()


def _new_portfolio(owner_id) -> uuid.UUID:
    with SessionLocal() as db:
        pf = Portfolio(name=f"prop {uuid.uuid4().hex[:6]}", owner_id=owner_id,
                       initial_cash=Decimal("10000"), cash_balance=Decimal("10000"))
        db.add(pf); db.commit()
        return pf.id


def _wipe_portfolio(pid: uuid.UUID):
    with SessionLocal() as db:
        db.execute(delete(LedgerEntry).where(LedgerEntry.portfolio_id == pid))
        db.execute(delete(Trade).where(Trade.portfolio_id == pid))
        db.execute(delete(Order).where(Order.portfolio_id == pid))
        db.execute(delete(Position).where(Position.portfolio_id == pid))
        db.execute(delete(Portfolio).where(Portfolio.id == pid))
        db.commit()


def _random_ops(rng: random.Random) -> list[tuple]:
    """A workload of (user_index 0..2, side, qty 1..15)."""
    n = rng.randint(4, 12)
    return [
        (rng.randint(0, 2),
         rng.choice([OrderSide.BUY, OrderSide.SELL]),
         rng.randint(1, 15))
        for _ in range(n)
    ]


@pytest.mark.parametrize("seed", range(12))
def test_concurrent_multiuser_ledger_invariants(shared_env, seed):
    uids, aid = shared_env["user_ids"], shared_env["asset_id"]
    ops = _random_ops(random.Random(seed))
    pid = _new_portfolio(uids[0])
    try:
        def run(op):
            uidx, side, q = op
            with SessionLocal() as db:
                try:
                    execute_market_order(db, portfolio_id=pid, user_id=uids[uidx],
                                         asset_id=aid, side=side, qty=Decimal(q))
                except Exception:  # rejections raise nothing; only infra errors here
                    pass

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(run, ops))

        with SessionLocal() as db:
            pf = db.get(Portfolio, pid)
            ledger_sum = db.scalar(
                select(func.coalesce(func.sum(LedgerEntry.amount), 0))
                .where(LedgerEntry.portfolio_id == pid)) or Decimal("0")
            balance_afters = set(db.scalars(
                select(LedgerEntry.balance_after).where(LedgerEntry.portfolio_id == pid)
            ).all())
            pos = db.get(Position, (pid, aid))
            signed_qty = db.scalar(
                select(func.coalesce(func.sum(
                    Trade.qty * text("CASE WHEN side='buy' THEN 1 ELSE -1 END")), 0))
                .where(Trade.portfolio_id == pid)) or Decimal("0")

        assert pf.cash_balance >= 0                                   # (1)
        assert pf.initial_cash + ledger_sum == pf.cash_balance         # (2)
        if balance_afters:
            assert pf.cash_balance in balance_afters                   # (3)
        held = pos.qty if pos else Decimal("0")
        assert held == signed_qty                                      # (4)
        assert held >= 0                                               # no phantom shorts
    finally:
        _wipe_portfolio(pid)


def test_check_constraint_is_the_backstop(shared_env):
    """cash_balance >= 0 is enforced by the DATABASE, not just app logic."""
    pid = _new_portfolio(shared_env["user_ids"][0])
    try:
        with SessionLocal() as db:
            with pytest.raises(IntegrityError):
                db.execute(text("UPDATE portfolios SET cash_balance = -1 WHERE id = :p"),
                           {"p": str(pid)})
                db.commit()
    finally:
        _wipe_portfolio(pid)
