"""Backtest execution + periodic maintenance jobs — in-process, no Celery.

In free-tier single-process mode, backtests run in FastAPI BackgroundTasks
(same process, after the response is sent) and the periodic jobs are driven by
app/scheduler.py. Each function here is a plain callable so both the scheduler
and the tests can invoke them directly.
"""
from __future__ import annotations

import logging
import time
import uuid

from app.backtest.runner import run_and_persist
from app.core.config import settings
from app.core.metrics import BACKTEST_DURATION

logger = logging.getLogger("backtest.tasks")


def _strategy_label(backtest_id: uuid.UUID) -> str:
    """Strategy key for the metric label; never lets a lookup failure break the
    run. Bounded cardinality: values come from the fixed strategy registries."""
    from app.db.session import SessionLocal
    from app.models import Backtest
    try:
        with SessionLocal() as db:
            bt = db.get(Backtest, backtest_id)
            return (bt.config or {}).get("strategy", "unknown") if bt else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def execute_backtest(backtest_id: uuid.UUID) -> None:
    """Run one backtest to completion (called via BackgroundTasks). The
    per-job memory rlimit of the old worker is intentionally NOT applied here —
    it would cap the whole web process. Admission control (the working-set 422
    at submit time) is the memory guard in single-process mode."""
    strategy = _strategy_label(backtest_id)
    start = time.perf_counter()
    try:
        run_and_persist(backtest_id)
    except Exception:
        BACKTEST_DURATION.labels(strategy, "failed").observe(time.perf_counter() - start)
        logger.exception("backtest %s failed", backtest_id)
        return
    BACKTEST_DURATION.labels(strategy, "completed").observe(time.perf_counter() - start)


# ---------------------------------------------------- periodic jobs --------
def snapshot_equity() -> dict:
    import time as time_mod

    from app.core.metrics import SNAPSHOT_LAST_SUCCESS
    from app.db.session import SessionLocal
    from app.services.snapshots import snapshot_portfolio_equity

    with SessionLocal() as db:
        n = snapshot_portfolio_equity(db)
    SNAPSHOT_LAST_SUCCESS.set(time_mod.time())
    return {"snapshots": n}


def refresh_fx() -> dict:
    from app.services.fx import refresh_fx_rates

    return refresh_fx_rates()


def append_daily_bars() -> dict:
    """Incremental daily-bar refresh for every active equity: fetch the last few
    sessions from yfinance and upsert (idempotent on the PK). Out-of-hours runs
    are cheap no-ops (already-stored sessions skip)."""
    from sqlalchemy import select

    from app.data.backfill import backfill_yfinance
    from app.db.session import SessionLocal
    from app.models import Asset
    from app.models.enums import AssetClass, Timeframe

    appended, failed = 0, 0
    with SessionLocal() as db:
        rows = db.execute(
            select(Asset.symbol, Asset.exchange, Asset.asset_class)
            .where(Asset.is_active,
                   Asset.asset_class.in_([AssetClass.US_EQUITY, AssetClass.IN_EQUITY]))
        ).all()
    for symbol, exchange, klass in rows:
        try:
            appended += backfill_yfinance(symbol, exchange, klass,
                                          timeframe=Timeframe.D1, period="5d") or 0
        except Exception:  # noqa: BLE001 — one bad ticker never sinks the sweep
            failed += 1
    if appended or failed:
        logger.info("daily bar append: +%d bars, %d failures", appended, failed)
    return {"appended": appended, "failed": failed}


def relay_outbox() -> dict:
    from app.services.events import relay_outbox as _relay
    from app.core.metrics import OUTBOX_PENDING

    out = _relay()
    if out["published"]:
        logger.warning("outbox relay re-published %d event(s) missed by a "
                       "crashed fast path", out["published"])
    try:
        from sqlalchemy import func, select
        from app.db.session import SessionLocal
        from app.models import OutboxEvent
        with SessionLocal() as db:
            OUTBOX_PENDING.set(db.scalar(
                select(func.count()).select_from(OutboxEvent)
                .where(OutboxEvent.published_at.is_(None))) or 0)
    except Exception:  # noqa: BLE001
        pass
    return out


def reap_dead_backtests() -> dict:
    from app.db.session import SessionLocal
    from app.services.reaper import reap_dead_backtests as _reap

    with SessionLocal() as db:
        n = _reap(db)
    if n:
        logger.warning("reaper healed %d orphaned backtest(s)", n)
    return {"reaped": n}


def poll_equity_ticks() -> dict:
    from app.db.session import SessionLocal
    from app.services.equity_poll import poll_equity_ticks as _poll

    with SessionLocal() as db:
        n = _poll(db)
    return {"ticks": n}


def finish_expired_challenges() -> dict:
    from app.db.session import SessionLocal
    from app.services.challenges import finish_expired

    with SessionLocal() as db:
        n = finish_expired(db)
    if n:
        logger.info("finished %d expired challenge(s)", n)
    return {"finished": n}
