"""Celery app + backtest task with a per-job memory cap.

The heavy pandas work runs OUT of the web process, on a Celery worker fed by
Redis. Each task sets an address-space rlimit (RLIMIT_AS) at start, so a
runaway backtest (e.g. 20 symbols × 5y of minute bars) is killed with a clean
MemoryError instead of OOM-ing the box and taking every other job down.

`task_always_eager` can be flipped on in tests to run inline without a worker.
"""
from __future__ import annotations

import logging
import platform
import resource
import time
import uuid

from celery import Celery
from celery.signals import worker_ready

from app.backtest.runner import run_and_persist
from app.core.config import settings
from app.core.metrics import BACKTEST_DURATION

logger = logging.getLogger("backtest.tasks")

celery_app = Celery(
    "backtester",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    worker_max_tasks_per_child=20,   # recycle workers to bound memory creep
    # Runaway-job kill switch (infinite loop in BYOC code, hung data fetch):
    # soft limit raises SoftTimeLimitExceeded inside the task 30s before the
    # hard limit SIGKILLs the prefork child. RLIMIT_AS bounds memory; this
    # bounds TIME — both are needed, neither substitutes for the other.
    task_time_limit=settings.BACKTEST_TIME_LIMIT_S,
    task_soft_time_limit=max(settings.BACKTEST_TIME_LIMIT_S - 30, 1),
)

# Periodic equity snapshots feed the windowed (24h/7d) leaderboard. The beat
# scheduler is embedded in the worker (`celery worker -B`) — one process fewer
# to deploy, and a single worker is the deployment shape anyway.
celery_app.conf.beat_schedule = {
    "portfolio-equity-snapshots": {
        "task": "portfolio.snapshot_equity",
        "schedule": settings.EQUITY_SNAPSHOT_INTERVAL_MINUTES * 60.0,
    },
}


def _apply_memory_cap(mb: int) -> None:
    """Cap this process's address space. Linux enforces hard; macOS ignores
    RLIMIT_AS, so this is a no-op there (dev only) — the real cap runs in the
    Linux worker container."""
    if platform.system() != "Linux":
        return
    limit = mb * 1024 * 1024
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        resource.setrlimit(resource.RLIMIT_AS, (limit, hard if hard > 0 else limit))
    except (ValueError, OSError) as exc:
        logger.warning("could not set memory cap: %s", exc)


@worker_ready.connect
def _start_metrics_server(**_kwargs) -> None:
    """Expose this worker's Prometheus metrics (backtest durations) on its own
    port. The worker is a separate process from the web app, so it cannot share
    the web /metrics endpoint — each process is scraped independently.

    Celery's prefork pool runs tasks in forked CHILDREN, so their metric writes
    never reach this parent's in-memory registry. With PROMETHEUS_MULTIPROC_DIR
    set (the worker container sets it), prometheus_client writes values to mmap
    files instead and we serve a MultiProcessCollector that merges them — the
    standard fix for prefork/gunicorn-style workers. Without the env var (bare
    local dev), we serve the parent registry and child samples are lost; that's
    a documented dev-only limitation, not worth a hard dependency on the dir.
    """
    import os
    import time as time_mod

    from prometheus_client import start_http_server

    from app.core.metrics import SNAPSHOT_LAST_SUCCESS

    # Seed the freshness gauge at boot: the beat loop is alive and its first
    # tick is at most one interval away. Without this a cold stack flaps
    # EquitySnapshotStale (gauge reads 0 -> "stale since 1970") until the
    # first tick lands.
    SNAPSHOT_LAST_SUCCESS.set(time_mod.time())
    try:
        if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
            from prometheus_client import CollectorRegistry, multiprocess
            registry = CollectorRegistry()
            multiprocess.MultiProcessCollector(registry)
            start_http_server(settings.WORKER_METRICS_PORT, registry=registry)
        else:
            start_http_server(settings.WORKER_METRICS_PORT)
        logger.info("worker metrics on :%d/metrics", settings.WORKER_METRICS_PORT)
    except OSError as exc:  # port taken (e.g. second local worker) — not fatal
        logger.warning("worker metrics server not started: %s", exc)


def _strategy_label(backtest_id: uuid.UUID) -> str:
    """Strategy key for the metric label; never lets a lookup failure break the
    task. Bounded cardinality: values come from the fixed strategy registries."""
    from app.db.session import SessionLocal
    from app.models import Backtest
    try:
        with SessionLocal() as db:
            bt = db.get(Backtest, backtest_id)
            return (bt.config or {}).get("strategy", "unknown") if bt else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


@celery_app.task(name="portfolio.snapshot_equity")
def snapshot_equity_task() -> dict:
    import time as time_mod

    from app.core.metrics import SNAPSHOT_LAST_SUCCESS
    from app.db.session import SessionLocal
    from app.services.snapshots import snapshot_portfolio_equity

    with SessionLocal() as db:
        n = snapshot_portfolio_equity(db)
    SNAPSHOT_LAST_SUCCESS.set(time_mod.time())
    return {"snapshots": n}


@celery_app.task(name="backtest.run", bind=True, max_retries=0)
def run_backtest_task(self, backtest_id: str) -> dict:
    _apply_memory_cap(settings.BACKTEST_MEMORY_CAP_MB)
    bt_id = uuid.UUID(backtest_id)
    strategy = _strategy_label(bt_id)
    start = time.perf_counter()
    try:
        run_and_persist(bt_id)
    except Exception:
        BACKTEST_DURATION.labels(strategy, "failed").observe(time.perf_counter() - start)
        raise
    BACKTEST_DURATION.labels(strategy, "completed").observe(time.perf_counter() - start)
    return {"backtest_id": backtest_id, "status": "completed"}
