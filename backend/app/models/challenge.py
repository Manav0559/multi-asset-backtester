import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Integer, Numeric, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import ChallengeStatus


class Challenge(Base):
    """A consent-based, head-to-head performance competition between two users
    on portfolios they each choose. There is NO global leaderboard: a challenge
    exists only when BOTH parties opt in, and each participant may see only a
    whitelisted set of AGGREGATES about the other (never positions/trades/code).

    Lifecycle: challenger creates (pending, opponent_portfolio null) → opponent
    accepts (active, both baselines snapshotted, end_at set) or declines; the
    challenger may cancel while pending; a beat job finishes it at end_at and
    freezes `final_metrics` so results are immutable even as trading continues.
    """

    __tablename__ = "challenges"
    __table_args__ = (
        CheckConstraint("challenger_id <> opponent_id", name="challenge_distinct_users"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    status: Mapped[ChallengeStatus] = mapped_column(
        Enum(ChallengeStatus, name="challenge_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False, server_default=ChallengeStatus.PENDING.value, index=True,
    )

    challenger_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    challenger_portfolio_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False)
    opponent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    opponent_portfolio_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("portfolios.id", ondelete="CASCADE"))

    duration_days: Mapped[int] = mapped_column(Integer, nullable=False)
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    # Equity baselines snapshotted at accept-time (normalize both curves to 100).
    challenger_baseline: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    opponent_baseline: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))

    # Frozen at finish: {challenger:{...}, opponent:{...}, winner_id}. Immutable
    # afterward, so results don't drift as the underlying portfolios keep trading.
    final_metrics: Mapped[dict | None] = mapped_column(JSONB)
    winner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
