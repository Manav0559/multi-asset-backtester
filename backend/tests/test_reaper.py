"""Reaper heals backtest rows orphaned by a hard-killed worker."""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.core.config import settings
from app.db.session import SessionLocal
from app.models import Backtest, StrategyVersion, Strategy, User
from app.models.enums import BacktestStatus
from app.services.reaper import reap_dead_backtests


def _make_running_backtest(started_delta_s: int) -> uuid.UUID:
    """A RUNNING backtest whose started_at is `started_delta_s` in the past."""
    with SessionLocal() as db:
        u = User(email=f"reap_{uuid.uuid4().hex[:8]}@x.com",
                 username=f"reap_{uuid.uuid4().hex[:8]}", hashed_password="x")
        db.add(u); db.flush()
        strat = Strategy(user_id=u.id, name=f"reap {uuid.uuid4().hex[:6]}")
        db.add(strat); db.flush()
        sv = StrategyVersion(strategy_id=strat.id, version=1, code="", params={})
        db.add(sv); db.flush()
        bt = Backtest(
            user_id=u.id, strategy_version_id=sv.id, status=BacktestStatus.RUNNING,
            config={"strategy": "sma_crossover"},
            started_at=datetime.now(timezone.utc) - timedelta(seconds=started_delta_s),
        )
        db.add(bt); db.commit()
        return bt.id, u.id


def _cleanup(uid):
    with SessionLocal() as db:
        db.execute(delete(Backtest).where(Backtest.user_id == uid))
        sids = db.scalars(select(Strategy.id).where(Strategy.user_id == uid)).all()
        db.execute(delete(StrategyVersion).where(StrategyVersion.strategy_id.in_(sids)))
        db.execute(delete(Strategy).where(Strategy.user_id == uid))
        db.execute(delete(User).where(User.id == uid))
        db.commit()


def test_reaper_heals_stale_running_job():
    # Older than time_limit + grace ⇒ reaped.
    stale = settings.BACKTEST_TIME_LIMIT_S + 300
    bt_id, uid = _make_running_backtest(stale)
    try:
        n = reap_dead_backtests(SessionLocal())
        assert n >= 1
        with SessionLocal() as db:
            bt = db.get(Backtest, bt_id)
        assert bt.status == BacktestStatus.FAILED
        assert "reaped" in bt.error
        assert bt.finished_at is not None
    finally:
        _cleanup(uid)


def test_reaper_leaves_fresh_running_job_alone():
    # A job that just started must NOT be reaped.
    bt_id, uid = _make_running_backtest(5)
    try:
        reap_dead_backtests(SessionLocal())
        with SessionLocal() as db:
            bt = db.get(Backtest, bt_id)
        assert bt.status == BacktestStatus.RUNNING  # untouched
    finally:
        _cleanup(uid)
