"""Order execution — the shared-ledger core.

`execute_market_order` is the single, serialized path that mutates a
portfolio's shared cash balance. Its correctness contract:

  1. One transaction. First statement is `SELECT ... FOR UPDATE` on the
     portfolios row, so concurrent orders on the SAME portfolio serialize;
     orders on DIFFERENT portfolios never contend.
  2. Validate against the LOCKED balance (buy) or the LOCKED position
     (sell) — never a stale read. Insufficient funds/holdings => the order
     is recorded as REJECTED and committed (audit trail), not silently
     dropped, and the caller learns why.
  3. On fill: write order(filled) + trade + position upsert + signed
     ledger entry (with balance_after) + bump portfolios.version, all in
     the same transaction. The CHECK (cash_balance >= 0) constraint is the
     last-resort backstop.
  4. Return a result the caller publishes to portfolio:{id} AFTER commit.

Everything here is synchronous SQLAlchemy; the FastAPI route runs it in a
worker thread and does the Redis publish once it returns.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import LedgerEntry, Order, Portfolio, Position, Trade
from app.models.enums import LedgerEntryType, OrderSide, OrderStatus, OrderType
from app.services.pricing import latest_price

_CENTS = Decimal("0.01")


class ExecutionError(Exception):
    """Caller-facing failure that is NOT a business rejection (e.g. no price,
    portfolio missing). Business rejections come back as a result object."""


@dataclass
class ExecutionResult:
    order_id: uuid.UUID
    status: OrderStatus
    reason: str | None
    fill_price: Decimal | None
    filled_qty: Decimal | None
    cash_balance: Decimal
    version: int

    @property
    def filled(self) -> bool:
        return self.status == OrderStatus.FILLED

    def to_event(self, portfolio_id: uuid.UUID, user_id: uuid.UUID,
                 asset_id: int, side: OrderSide) -> dict:
        """Payload published to portfolio:{id} so every collaborator's UI
        updates live (shared cash + who did what)."""
        return {
            "type": "order",
            "portfolio_id": str(portfolio_id),
            "order_id": str(self.order_id),
            "user_id": str(user_id),
            "asset_id": asset_id,
            "side": side.value,
            "status": self.status.value,
            "reason": self.reason,
            "fill_price": str(self.fill_price) if self.fill_price is not None else None,
            "filled_qty": str(self.filled_qty) if self.filled_qty is not None else None,
            "cash_balance": str(self.cash_balance),
            "version": self.version,
        }


def _commission(notional: Decimal) -> Decimal:
    if settings.COMMISSION_BPS <= 0:
        return Decimal("0.00")
    bps = Decimal(str(settings.COMMISSION_BPS)) / Decimal("10000")
    return (notional * bps).quantize(_CENTS, rounding=ROUND_HALF_UP)


def execute_market_order(
    db: Session,
    *,
    portfolio_id: uuid.UUID,
    user_id: uuid.UUID,
    asset_id: int,
    side: OrderSide,
    qty: Decimal,
) -> ExecutionResult:
    """Execute one market order atomically. Manages its own transaction."""
    if qty <= 0:
        raise ExecutionError("qty must be positive")

    # (1) Lock the shared-cash row FIRST. Everything below reads post-lock.
    portfolio = db.execute(
        select(Portfolio).where(Portfolio.id == portfolio_id).with_for_update()
    ).scalar_one_or_none()
    if portfolio is None:
        raise ExecutionError("portfolio not found")

    price = latest_price(db, asset_id)
    if price is None:
        raise ExecutionError("no market price available for asset")

    notional = (price * qty).quantize(_CENTS, rounding=ROUND_HALF_UP)
    commission = _commission(notional)

    position = db.get(Position, (portfolio_id, asset_id))

    def _reject(reason: str) -> ExecutionResult:
        order = Order(
            portfolio_id=portfolio_id, user_id=user_id, asset_id=asset_id,
            side=side, order_type=OrderType.MARKET, qty=qty,
            status=OrderStatus.REJECTED, reject_reason=reason,
        )
        db.add(order)
        db.commit()
        return ExecutionResult(order.id, OrderStatus.REJECTED, reason, None, None,
                               portfolio.cash_balance, portfolio.version)

    # (2) Validate against the LOCKED state.
    if side == OrderSide.BUY:
        required = notional + commission
        if portfolio.cash_balance < required:
            return _reject(
                f"insufficient funds: need {required}, have {portfolio.cash_balance}"
            )
    else:  # SELL
        held = position.qty if position else Decimal("0")
        if not settings.ALLOW_SHORTING and held < qty:
            return _reject(f"insufficient position: hold {held}, tried to sell {qty}")

    # (3) Fill. Record order + trade.
    order = Order(
        portfolio_id=portfolio_id, user_id=user_id, asset_id=asset_id,
        side=side, order_type=OrderType.MARKET, qty=qty,
        status=OrderStatus.FILLED,
    )
    db.add(order)
    db.flush()  # need order.id for the trade FK

    trade = Trade(
        order_id=order.id, portfolio_id=portfolio_id, user_id=user_id,
        asset_id=asset_id, side=side, qty=qty, fill_price=price, commission=commission,
    )
    db.add(trade)
    db.flush()

    # Position + cash mutation.
    if side == OrderSide.BUY:
        portfolio.cash_balance = portfolio.cash_balance - notional - commission
        if position is None:
            position = Position(portfolio_id=portfolio_id, asset_id=asset_id,
                                qty=Decimal("0"), avg_entry_price=Decimal("0"))
            db.add(position)
        new_qty = position.qty + qty
        # weighted-average cost basis
        position.avg_entry_price = (
            (position.qty * position.avg_entry_price + qty * price) / new_qty
        ).quantize(Decimal("0.00000001"))
        position.qty = new_qty
        entry_type = LedgerEntryType.TRADE_BUY
        cash_delta = -(notional + commission)
    else:  # SELL
        portfolio.cash_balance = portfolio.cash_balance + notional - commission
        realized = ((price - position.avg_entry_price) * qty).quantize(_CENTS)
        position.realized_pnl = position.realized_pnl + realized
        position.qty = position.qty - qty
        if position.qty == 0:
            position.avg_entry_price = Decimal("0")
        entry_type = LedgerEntryType.TRADE_SELL
        cash_delta = notional - commission

    # Signed ledger entry with running balance (self-verifying audit trail).
    db.add(LedgerEntry(
        portfolio_id=portfolio_id, trade_id=trade.id, entry_type=entry_type,
        amount=cash_delta.quantize(_CENTS), balance_after=portfolio.cash_balance,
        note=f"{side.value} {qty} @ {price}",
    ))

    # (3b) Bump version so clients can detect stale state after reconnect.
    portfolio.version = portfolio.version + 1

    order.filled_at = trade.executed_at
    db.commit()

    return ExecutionResult(order.id, OrderStatus.FILLED, None, price, qty,
                           portfolio.cash_balance, portfolio.version)
