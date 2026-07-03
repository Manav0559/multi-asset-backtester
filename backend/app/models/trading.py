import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import LedgerEntryType, OrderSide, OrderStatus, OrderType


class Order(Base):
    """A paper order submitted by a member of a portfolio. `user_id`
    records WHO acted — essential in shared portfolios for the activity
    feed and per-collaborator attribution."""

    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("qty > 0", name="qty_positive"),
        Index("ix_orders_portfolio_created", "portfolio_id", "created_at"),
        # Exactly-once under retries. NULL keys never dedupe (Postgres treats
        # NULLs as distinct), so non-idempotent callers are unaffected.
        UniqueConstraint("portfolio_id", "idempotency_key",
                         name="uq_orders_portfolio_idempotency"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    asset_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    side: Mapped[OrderSide] = mapped_column(
        Enum(OrderSide, name="order_side", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    order_type: Mapped[OrderType] = mapped_column(
        Enum(OrderType, name="order_type", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        server_default=OrderType.MARKET.value,
    )
    qty: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)  # fractional for crypto
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    # Client-supplied dedup key; reused across retries of the same intent.
    idempotency_key: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, name="order_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        server_default=OrderStatus.PENDING.value,
        index=True,
    )
    reject_reason: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Trade(Base):
    """An executed fill. Immutable once written."""

    __tablename__ = "trades"
    __table_args__ = (
        Index("ix_trades_portfolio_executed", "portfolio_id", "executed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    asset_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False
    )
    side: Mapped[OrderSide] = mapped_column(
        Enum(OrderSide, name="order_side", values_callable=lambda e: [m.value for m in e],
             create_type=False),
        nullable=False,
    )
    qty: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    fill_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    commission: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, server_default="0")
    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Position(Base):
    """Current holdings per portfolio per asset. Updated in the same
    locked transaction as the cash mutation."""

    __tablename__ = "positions"

    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("portfolios.id", ondelete="CASCADE"), primary_key=True
    )
    asset_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("assets.id", ondelete="RESTRICT"), primary_key=True
    )
    qty: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False, server_default="0")
    avg_entry_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, server_default="0")
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, server_default="0")

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class LedgerEntry(Base):
    """Append-only audit trail of every cash movement in a portfolio.
    `balance_after` makes the ledger self-verifying: replaying entries
    must reproduce portfolios.cash_balance exactly."""

    __tablename__ = "ledger_entries"
    __table_args__ = (
        Index("ix_ledger_entries_portfolio_created", "portfolio_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False
    )
    trade_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trades.id", ondelete="SET NULL")
    )
    entry_type: Mapped[LedgerEntryType] = mapped_column(
        Enum(LedgerEntryType, name="ledger_entry_type", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)          # signed: +credit / -debit
    balance_after: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    note: Mapped[str | None] = mapped_column(String(255))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
