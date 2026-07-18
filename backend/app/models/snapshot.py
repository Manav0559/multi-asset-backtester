import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PortfolioEquitySnapshot(Base):
    """Periodic equity mark per portfolio, appended by the scheduler job.

    Time-windowed leaderboard returns (24h / 7d) are computed from these rows:
    the baseline for a window is the latest snapshot at or before the window
    start. The composite PK (portfolio_id, time) is deliberately ordered so the
    backing btree serves exactly that lookup — `WHERE portfolio_id = ? AND
    time <= ? ORDER BY time DESC LIMIT 1` — with no secondary index.

    Marking matches services/equity.py's terminal point: cash + positions at
    each asset's latest close (entry price when an asset has no bars yet).
    """

    __tablename__ = "portfolio_equity_snapshots"

    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("portfolios.id", ondelete="CASCADE"), primary_key=True
    )
    time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)

    cash: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    equity: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
