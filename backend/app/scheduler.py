"""Lightweight in-process periodic scheduler — the Celery-beat replacement.

Each job is a plain sync function run off the event loop (asyncio.to_thread) on
a fixed interval. A job that raises is logged and retried on its next tick,
never taking the loop or its siblings down. Started/stopped by the app lifespan.
"""
from __future__ import annotations

import asyncio
import logging

from app.backtest import tasks
from app.core.config import settings

logger = logging.getLogger("scheduler")

# (name, interval_seconds, callable). Intervals mirror the old beat schedule.
_JOBS = [
    ("snapshot_equity", settings.EQUITY_SNAPSHOT_INTERVAL_MINUTES * 60.0, tasks.snapshot_equity),
    ("reap_dead", 60.0, tasks.reap_dead_backtests),
    ("finish_challenges", 60.0, tasks.finish_expired_challenges),
    ("relay_outbox", 10.0, tasks.relay_outbox),
    ("refresh_fx", 3600.0, tasks.refresh_fx),
    ("append_daily_bars", 6 * 3600.0, tasks.append_daily_bars),
]
if settings.EQUITY_POLL_ENABLED:
    _JOBS.append(("poll_equity_ticks", float(settings.EQUITY_POLL_INTERVAL_SECONDS),
                  tasks.poll_equity_ticks))


async def _run_job(name: str, interval: float, fn) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(fn)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — one bad tick never stops the schedule
            logger.exception("scheduled job %s failed", name)


class Scheduler:
    def __init__(self) -> None:
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        for name, interval, fn in _JOBS:
            self._tasks.append(asyncio.create_task(_run_job(name, interval, fn),
                                                   name=f"job-{name}"))
        logger.info("scheduler started (%d jobs)", len(self._tasks))
        # Seed the snapshot-freshness gauge so a cold start doesn't flap the
        # staleness alert until the first tick lands.
        from app.core.metrics import SNAPSHOT_LAST_SUCCESS
        import time
        SNAPSHOT_LAST_SUCCESS.set(time.time())

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        self._tasks.clear()


scheduler = Scheduler()
