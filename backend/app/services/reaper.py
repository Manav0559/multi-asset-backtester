"""Reap dead backtest jobs.

A backtest row goes RUNNING when the background task picks it up and
COMPLETED/FAILED when it finishes. But a hard kill — cgroup OOM (SIGKILL), a
crashed container, `kill -9` — runs no except-path, so the row is orphaned in
RUNNING forever. This sweep is the backstop: any RUNNING row older than the
task time budget plus a grace margin is marked FAILED, because no honest job
runs that long. In single-process mode this is also the only enforcement of
BACKTEST_TIME_LIMIT_S — BackgroundTasks has nothing to SIGKILL.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Backtest
from app.models.enums import BacktestStatus


def reap_dead_backtests(db: Session, grace_seconds: int = 120) -> int:
    """Mark orphaned RUNNING backtests FAILED. Returns rows healed."""
    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=settings.BACKTEST_TIME_LIMIT_S + grace_seconds
    )
    orphans = db.execute(
        select(Backtest).where(
            Backtest.status == BacktestStatus.RUNNING,
            Backtest.started_at.is_not(None),
            Backtest.started_at < cutoff,
        )
    ).scalars().all()
    for bt in orphans:
        bt.status = BacktestStatus.FAILED
        bt.error = "worker died mid-run (no completion within time limit); reaped"
        bt.finished_at = datetime.now(timezone.utc)
    db.commit()
    return len(orphans)
