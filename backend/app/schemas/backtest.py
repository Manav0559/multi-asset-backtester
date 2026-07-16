import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class StrategyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    code: str = Field(default="", description="source for user strategies; empty for built-ins")
    params: dict | None = None


class BacktestCreate(BaseModel):
    strategy_version_id: uuid.UUID
    name: str | None = Field(default=None, max_length=120)  # user's label for the run
    asset_id: int | None = None                 # single-asset strategies
    asset_ids: list[int] | None = None          # multi-asset / long-short strategies
    timeframe: str = "1d"
    strategy: str = Field(description="strategy key, e.g. sma_crossover, cross_sectional_momentum")
    params: dict | None = None
    code: str | None = None                     # BYOC source (strategy == "custom_code")
    start: str | None = None
    end: str | None = None
    initial_capital: float = 100_000.0
    commission_bps: float = 0.0
    slippage_bps: float = 5.0                   # per-side fill friction (CostModel)
    n_trials: int = 1
    # Long/short portfolio controls (ignored by single-asset strategies)
    borrow_bps_annual: float = 0.0
    max_gross_leverage: float | None = None


class YearlyResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    year: int
    return_pct: Decimal | None
    max_drawdown_pct: Decimal | None
    sharpe: Decimal | None
    sortino: Decimal | None
    volatility_pct: Decimal | None
    trade_count: int | None
    win_rate_pct: Decimal | None


class BacktestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: str
    config: dict
    error: str | None
    total_return_pct: Decimal | None
    cagr_pct: Decimal | None
    sharpe: Decimal | None
    sortino: Decimal | None
    deflated_sharpe: Decimal | None
    max_drawdown_pct: Decimal | None
    trade_count: int | None
    win_rate_pct: Decimal | None
    diagnostics: dict | None = None
    created_at: datetime
    finished_at: datetime | None
