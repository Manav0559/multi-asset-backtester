import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import BacktestStatus


class Backtest(Base):
    """One backtest run of a specific strategy version.

    `config` (JSONB) captures the full reproducibility envelope:
    symbols, timeframe, date range, initial capital, commission model,
    slippage model, engine version. Overall headline metrics live here;
    the per-year breakdown lives in BacktestYearlyResult.
    """

    __tablename__ = "backtests"
    __table_args__ = (
        Index("ix_backtests_user_created", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    strategy_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("strategy_versions.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    status: Mapped[BacktestStatus] = mapped_column(
        Enum(BacktestStatus, name="backtest_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        server_default=BacktestStatus.QUEUED.value,
        index=True,
    )
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)

    # Headline metrics (populated on completion)
    total_return_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    cagr_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    sharpe: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    sortino: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    deflated_sharpe: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))  # overfitting guard
    max_drawdown_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    trade_count: Mapped[int | None] = mapped_column(Integer)
    win_rate_pct: Mapped[Decimal | None] = mapped_column(Numeric(7, 4))

    # Equity curve, downsampled for charting (list of [ts, equity] pairs).
    # Full-resolution curves can move to object storage later without a schema change.
    equity_curve: Mapped[list | None] = mapped_column(JSONB)

    # Strategy-specific outputs (ML: oos_accuracy, feature_importance, ...).
    diagnostics: Mapped[dict | None] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BacktestYearlyResult(Base):
    """Performance Slicing: one row per calendar year of the backtest,
    powering the YoY table in the UI (returns, drawdown, risk ratios)."""

    __tablename__ = "backtest_yearly_results"
    __table_args__ = (
        UniqueConstraint("backtest_id", "year", name="uq_backtest_yearly_results_backtest_year"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    backtest_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("backtests.id", ondelete="CASCADE"), nullable=False, index=True
    )
    year: Mapped[int] = mapped_column(Integer, nullable=False)

    return_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    max_drawdown_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    sharpe: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))       # annualized
    sortino: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))      # annualized
    volatility_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    trade_count: Mapped[int | None] = mapped_column(Integer)
    win_rate_pct: Mapped[Decimal | None] = mapped_column(Numeric(7, 4))
