"""Application settings loaded from environment / .env file."""
import os

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database
    DATABASE_URL: str = "postgresql+psycopg2://postgres:postgres@localhost:5433/backtester"

    @model_validator(mode="before")
    @classmethod
    def _assemble_database_url(cls, values: dict) -> dict:
        """Managed hosts (Render, etc.) inject discrete POSTGRES_* vars rather
        than a ready-made SQLAlchemy URL. When DATABASE_URL is not set explicitly
        but POSTGRES_HOST is, build it — the driver prefix and psycopg2 dialect
        are ours to enforce, not the platform's."""
        if "DATABASE_URL" not in os.environ and os.environ.get("POSTGRES_HOST"):
            user = os.environ.get("POSTGRES_USER", "postgres")
            pw = os.environ.get("POSTGRES_PASSWORD", "")
            host = os.environ["POSTGRES_HOST"]
            port = os.environ.get("POSTGRES_PORT", "5432")
            db = os.environ.get("POSTGRES_DB", "backtester")
            values["DATABASE_URL"] = f"postgresql+psycopg2://{user}:{pw}@{host}:{port}/{db}"
        return values

    # App
    APP_NAME: str = "Backtester"
    DEBUG: bool = False


    # Paper trading execution
    COMMISSION_BPS: float = 0.0      # commission in basis points of notional
    ALLOW_SHORTING: bool = True      # sells may open/extend a short (negative position)
    # Buying power = cash + (MAX_LEVERAGE-1)*max(equity,0). 1.0 = cash-only (no
    # leverage); 2.0 = buy/hold up to 2x account equity. Enforced under the
    # portfolio row lock in execution.py — the ledger has no cash>=0 floor now.
    MAX_LEVERAGE: float = 2.0
    # Fixed slippage charged per side on live fills AND backtest turnover: the
    # fill price is worsened by this many bps (buys pay up, sells receive less).
    # A close-price fill with zero friction reports returns nobody can capture.
    SLIPPAGE_BPS: float = 5.0
    # Initial margin for opening/extending a short: the short-opening notional
    # must be backed by this fraction of value (Reg-T style 150% = proceeds
    # + 50% margin). The (requirement - 1) excess is charged against buying
    # power under the same row lock, capping maximum short exposure.
    SHORT_MARGIN_REQUIREMENT: float = 1.5

    # Backtesting
    # Wall-clock budget for a single backtest task. In-process BackgroundTasks
    # can't SIGKILL a runaway job, so this bounds the reaper instead: any row
    # RUNNING past limit + grace is marked FAILED (no honest job runs longer).
    BACKTEST_TIME_LIMIT_S: int = 600
    BACKTEST_RISK_FREE_RATE: float = 0.0  # annual, for Sharpe/Sortino
    BACKTEST_DEFAULT_TRIALS: int = 1      # strategy variants tried, for Deflated Sharpe
    # Admission control: max ESTIMATED working set a job may claim. In
    # single-process mode this is the memory guard (a per-job rlimit would cap
    # the whole web process) — reject in milliseconds at submit with an
    # actionable 422 instead of OOMing the API minutes later.
    BACKTEST_MAX_WORKING_SET_MB: int = 1024

    # WS hub heartbeat: clients treat ~3 missed beats as a dead link (show the
    # banner + force reconnect) and an epoch change as "hub restarted — resync".
    HUB_HEARTBEAT_SECONDS: float = 15.0

    # Windowed leaderboard: how often the scheduler snapshots portfolio equity.
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

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


settings = Settings()
