from datetime import datetime

from sqlalchemy import DateTime, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class FxRate(Base):
    """Latest spot FX per pair (e.g. USDINR = rupees per dollar), refreshed by
    an hourly beat task from yfinance ("USDINR=X"). The ledger converts every
    non-USD fill through this table — a missing/never-fetched rate REJECTS the
    trade rather than guessing, so `updated_at` is surfaced for honesty (the
    UI can badge conversions with the rate's age)."""

    __tablename__ = "fx_rates"

    pair: Mapped[str] = mapped_column(String(12), primary_key=True)
    rate: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        onupdate=func.now())
