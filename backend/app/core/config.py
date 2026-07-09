"""Application settings loaded from environment / .env file."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database
    DATABASE_URL: str = "postgresql+psycopg2://postgres:postgres@localhost:5433/backtester"

    # App
    APP_NAME: str = "Backtester"
    DEBUG: bool = False

    # Redis (tick bus + pub/sub fan-out). Host port 6380 per docker-compose.
    REDIS_URL: str = "redis://localhost:6380/0"

    # Streaming
    BINANCE_WS_URL: str = "wss://stream.binance.com:9443/stream"
    ALPACA_API_KEY: str = ""
    ALPACA_API_SECRET: str = ""
    ALPACA_FEED: str = "iex"  # "iex" (free) or "sip" (paid)
    YFINANCE_POLL_SECONDS: float = 3.0

    # Paper trading execution
    COMMISSION_BPS: float = 0.0      # commission in basis points of notional
    ALLOW_SHORTING: bool = False     # v1: reject sells beyond held quantity

    # Backtesting
    CELERY_BROKER_URL: str = "redis://localhost:6380/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6380/2"
    # Per-job ADDRESS-SPACE ceiling (RLIMIT_AS, Linux only). Address space is
    # much larger than RSS — xgboost alone fails to mmap libxgboost.so under
    # 1GB — so this is a runaway-job bound, not a working-set budget.
    BACKTEST_MEMORY_CAP_MB: int = 4096
    # Wall-clock kill switch for a single backtest task. This is what stops an
    # infinite loop in BYOC user code: prefork SIGKILLs the child at the hard
    # limit and records the task failed. Memory caps can't catch a tight loop.
    BACKTEST_TIME_LIMIT_S: int = 600
    BACKTEST_RISK_FREE_RATE: float = 0.0  # annual, for Sharpe/Sortino
    BACKTEST_DEFAULT_TRIALS: int = 1      # strategy variants tried, for Deflated Sharpe
    # Admission control: max ESTIMATED working set a job may claim. A policy
    # cap far under the RLIMIT_AS backstop — reject in milliseconds at submit
    # with an actionable 422 instead of OOM-killing after minutes in the worker.
    BACKTEST_MAX_WORKING_SET_MB: int = 1024

    # WS hub heartbeat: clients treat ~3 missed beats as a dead link (show the
    # banner + force reconnect) and an epoch change as "hub restarted — resync".
    HUB_HEARTBEAT_SECONDS: float = 15.0

    # Windowed leaderboard: how often the Celery beat task snapshots equity.
    EQUITY_SNAPSHOT_INTERVAL_MINUTES: int = 5

    # Delayed equity price poll (yfinance; NOT live — vendor-delayed ~15 min).
    EQUITY_POLL_ENABLED: bool = True
    EQUITY_POLL_INTERVAL_SECONDS: int = 20

    # Auth / JWT — MUST be overridden via env in any real deployment.
    # >= 32 bytes per RFC 7518 §3.2 for HS256
    JWT_SECRET: str = "dev-only-secret-change-me-in-production-0123456789"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # HTTP hardening / observability
    # CORS: comma-separated origins, or "*" for any (dev). Same-origin in prod via
    # the Next.js /api rewrite means this is normally locked down to the web host.
    CORS_ORIGINS: str = "*"
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_PER_MINUTE: int = 120
    # Celery worker's own Prometheus exposition port (separate process from web).
    WORKER_METRICS_PORT: int = 9200

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


settings = Settings()
