"""Prometheus metrics — exposed by the single web process at GET /metrics.

Metrics live in the default registry. The app runs as one process (backtests
via FastAPI BackgroundTasks, periodic jobs on the asyncio scheduler), so there
is no separate worker registry and no multiprocess collector — Prometheus
scrapes this one endpoint.

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


BACKTEST_DURATION = Histogram(
    "backtest_duration_seconds",
    "Wall-clock duration of a backtest run (FastAPI BackgroundTasks)",
    ["strategy", "status"],  # status: completed | failed
    buckets=(0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0),
)

SNAPSHOT_LAST_SUCCESS = Gauge(
    "equity_snapshot_last_success_timestamp_seconds",
    "Unix time of the last successful equity snapshot tick — alerting "
    "fires when this goes stale (scheduler dead, task crashing, DB unreachable)",
)

# ---- WS fan-out fabric (the previously-invisible layer) --------------------
WS_CLIENTS = Gauge(
    "ws_connected_clients",
    "WebSocket clients currently connected to this hub process",
)

WS_CONFLATED = Counter(
    "ws_conflated_frames_total",
    "Frames dropped by conflation (a newer frame replaced an undelivered one "
    "for a slow client) — by design for market data, but a rate spike means "
    "clients are falling behind",
    ["channel_class"],  # tick | depth | bar — fixed set
)

WS_OVERFLOW_DISCONNECTS = Counter(
    "ws_disconnects_overflow_total",
    "Clients disconnected because their must-deliver queue overflowed — each "
    "one had to full-resync over REST",
)

# ---- DB pool saturation (precedes every latency spike) ---------------------
DB_POOL_CHECKED_OUT = Gauge(
    "db_pool_checked_out",
    "SQLAlchemy connections currently checked out of the pool",
)

DB_POOL_SIZE = Gauge(
    "db_pool_size",
    "Configured SQLAlchemy pool size (for saturation ratio alerts)",
)

# ---- async backlog (the honest backpressure signals) ------------------------

OUTBOX_PENDING = Gauge(
    "outbox_pending_events",
    "Outbox rows not yet published — should drain within one relay tick; "
    "sustained >0 means the relay tick is dead or wedged",
)
