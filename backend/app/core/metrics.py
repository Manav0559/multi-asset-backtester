"""Prometheus metrics — shared by the web process and the Celery worker.

Metrics live in the default registry. The web process exposes them at
GET /metrics; the worker (a separate process, so a separate registry instance)
serves its own exposition endpoint on WORKER_METRICS_PORT via a `worker_ready`
signal in tasks.py. In production each process is scraped independently —
that's the standard multi-process Prometheus pattern (no pushgateway needed).

Route labels use the matched route TEMPLATE (`/backtests/{backtest_id}`), never
the raw path, so label cardinality stays bounded.
"""
from prometheus_client import Counter, Gauge, Histogram

HTTP_REQUESTS = Counter(
    "http_requests_total",
    "HTTP requests processed",
    ["method", "route", "status"],
)

HTTP_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

LEADERBOARD_QUERY_TIME = Histogram(
    "leaderboard_query_duration_seconds",
    "Wall-clock time of the leaderboard ranking query (SQL + serialization)",
    ["window"],  # 24h | 7d | all — fixed set, bounded cardinality
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)

BACKTEST_DURATION = Histogram(
    "backtest_duration_seconds",
    "Wall-clock duration of a backtest run on the worker",
    ["strategy", "status"],  # status: completed | failed
    buckets=(0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0),
)

SNAPSHOT_LAST_SUCCESS = Gauge(
    "equity_snapshot_last_success_timestamp_seconds",
    "Unix time of the last successful equity snapshot beat tick — alerting "
    "fires when this goes stale (beat dead, task crashing, DB unreachable)",
    # prefork: each child writes its own mmap value; scrape takes the newest.
    multiprocess_mode="max",
)
