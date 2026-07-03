import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import InviteStatus, PortfolioRole


class Portfolio(Base):
    """A paper-trading account. May be operated by multiple members who
    share ONE cash balance (the Shared Ledger Rule).

    Concurrency contract:
      - `cash_balance` is only ever mutated inside a transaction that
        first takes `SELECT ... FOR UPDATE` on this row.
      - `version` is bumped on every mutation; clients use it to detect
        stale state after WS reconnects.
      - The CHECK constraint is the DB-level backstop against a
        double-spend ever going negative, even under an app bug.
    """

    __tablename__ = "portfolios"
    __table_args__ = (
        CheckConstraint("cash_balance >= 0", name="cash_non_negative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    base_currency: Mapped[str] = mapped_column(String(8), nullable=False, server_default="USD")
    initial_cash: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    cash_balance: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    is_public: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")  # leaderboard visibility

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class PortfolioMember(Base):
    """Membership + role inside a shared portfolio. The owner also gets a
    row here (role=owner) so authorization is a single-table lookup."""

    __tablename__ = "portfolio_members"

    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("portfolios.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    role: Mapped[PortfolioRole] = mapped_column(
        Enum(PortfolioRole, name="portfolio_role", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        server_default=PortfolioRole.TRADER.value,
    )
    invited_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PortfolioInvite(Base):
    """Email-based invite flow into a shared portfolio."""

    __tablename__ = "portfolio_invites"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    inviter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    invitee_email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    role: Mapped[PortfolioRole] = mapped_column(
        Enum(PortfolioRole, name="portfolio_role", values_callable=lambda e: [m.value for m in e],
             create_type=False),  # type already created by PortfolioMember
        nullable=False,
        server_default=PortfolioRole.TRADER.value,
    )
    status: Mapped[InviteStatus] = mapped_column(
        Enum(InviteStatus, name="invite_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        server_default=InviteStatus.PENDING.value,
        index=True,
    )
    token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
