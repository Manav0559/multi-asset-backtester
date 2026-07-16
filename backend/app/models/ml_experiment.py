from datetime import datetime
from decimal import Decimal
from uuid import UUID as PyUUID

from sqlalchemy import DateTime, Index, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MlExperiment(Base):
    """One row per completed ML backtest: the model family, the hyperparameters
    it actually ran with (tuned or default), and the honest outcome metrics.
    This is the minimal experiment log — enough to answer "which configs have
    we tried and what did they really score?" without external tracking infra.
    ml_trials counts ATTEMPTS for the DSR correction; this records RESULTS."""

    __tablename__ = "ml_experiments"
    __table_args__ = (Index("ix_ml_experiments_model_id", "model_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    backtest_id: Mapped[PyUUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    model_id: Mapped[str] = mapped_column(String(64), nullable=False)
    params: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    oos_accuracy: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    brier_score: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    sharpe: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    deflated_sharpe: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
