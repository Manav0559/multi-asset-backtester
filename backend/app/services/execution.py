"""Order execution — the shared-ledger core.

`execute_market_order` is the single, serialized path that mutates a
portfolio's shared cash balance. Its correctness contract:

  1. One transaction. First statement is `SELECT ... FOR UPDATE` on the
     portfolios row, so concurrent orders on the SAME portfolio serialize;
     orders on DIFFERENT portfolios never contend.
  2. Validate against the LOCKED balance — a buy (opening a long OR buying a
     short back) needs cash; a sell may open/extend a short. Insufficient
     buying power => the order is recorded as REJECTED and committed (audit
     trail), not silently dropped, and the caller learns why.
  3. On fill: write order(filled) + trade + signed position upsert + signed
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
from sqlalchemy.exc import IntegrityError
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
    # Set on fills: the outbox row committed with the ledger entry (the route
    # marks it published after the fast-path Redis publish succeeds) and the
    # exact payload it holds — publish THIS, so fast path and relay emit
    # byte-identical events.
    outbox_id: int | None = None
    event: dict | None = None

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


def _result_from_existing(db: Session, order: Order, portfolio: Portfolio) -> ExecutionResult:
    """Reconstruct the original outcome of an already-processed order so a
    retry with the same idempotency key gets an identical response."""
    if order.status == OrderStatus.FILLED:
        trade = db.execute(select(Trade).where(Trade.order_id == order.id)).scalar_one()
        return ExecutionResult(order.id, OrderStatus.FILLED, None,
                               trade.fill_price, trade.qty,
                               portfolio.cash_balance, portfolio.version)
    return ExecutionResult(order.id, order.status, order.reject_reason, None, None,
                           portfolio.cash_balance, portfolio.version)


def execute_market_order(
    db: Session,
    *,
    portfolio_id: uuid.UUID,
    user_id: uuid.UUID,
    asset_id: int,
    side: OrderSide,
    qty: Decimal,
    idempotency_key: str | None = None,
) -> ExecutionResult:
    """Execute one market order atomically. Manages its own transaction.

    Idempotency: a retry carrying the same `idempotency_key` returns the
    original order's outcome without executing again. The pre-check runs
    INSIDE the portfolio row lock, so same-portfolio retries serialize and
    see the prior insert; the unique constraint is the backstop for a request
    that slips past the check on a separate connection."""
    if qty <= 0:
        raise ExecutionError("qty must be positive")

    # (1) Lock the shared-cash row FIRST. Everything below reads post-lock.
    portfolio = db.execute(
        select(Portfolio).where(Portfolio.id == portfolio_id).with_for_update()
    ).scalar_one_or_none()
    if portfolio is None:
        raise ExecutionError("portfolio not found")

    # (1b) Idempotency fast path: same key already processed on this portfolio.
    if idempotency_key is not None:
        prior = db.execute(
            select(Order).where(Order.portfolio_id == portfolio_id,
                                Order.idempotency_key == idempotency_key)
        ).scalar_one_or_none()
        if prior is not None:
            return _result_from_existing(db, prior, portfolio)

    price = latest_price(db, asset_id)
    if price is None:
        raise ExecutionError("no market price available for asset")

    # Multi-currency: `price` is in the ASSET's quote currency (NSE → INR);
    # cash is USD. Convert the notional through the FX rate, resolved on demand
    # (stored → live fetch → fallback constant) so a cold feed can't block an
    # international trade. fill_price/avg_entry stay in asset currency; ledger
    # cash in USD. None only for a currency the platform can't convert at all.
    from app.models import Asset as _Asset
    from app.services.fx import ensure_usd_rate
    asset_ccy = db.scalar(select(_Asset.currency).where(_Asset.id == asset_id)) or "USD"
    fx = ensure_usd_rate(db, asset_ccy)
    if fx is None or fx <= 0:
        raise ExecutionError(f"unsupported settlement currency {asset_ccy}")

    notional_ccy = (price * qty)
    notional = (notional_ccy / fx).quantize(_CENTS, rounding=ROUND_HALF_UP)  # USD
    commission = _commission(notional)

    position = db.get(Position, (portfolio_id, asset_id))

    def _reject(reason: str) -> ExecutionResult:
        order = Order(
            portfolio_id=portfolio_id, user_id=user_id, asset_id=asset_id,
            side=side, order_type=OrderType.MARKET, qty=qty,
            status=OrderStatus.REJECTED, reject_reason=reason,
            idempotency_key=idempotency_key,
        )
        db.add(order)
        try:
            db.commit()
        except IntegrityError:  # concurrent retry won the race — return its outcome
            db.rollback()
            return _replay_prior()
        return ExecutionResult(order.id, OrderStatus.REJECTED, reason, None, None,
                               portfolio.cash_balance, portfolio.version)

    def _replay_prior() -> ExecutionResult:
        """After a unique-violation rollback, re-read the winning order (fresh
        transaction) and return its outcome. Only reachable with a key set."""
        pf = db.execute(select(Portfolio).where(Portfolio.id == portfolio_id)).scalar_one()
        prior = db.execute(
            select(Order).where(Order.portfolio_id == portfolio_id,
                                Order.idempotency_key == idempotency_key)
        ).scalar_one()
        return _result_from_existing(db, prior, pf)

    # (2) Validate against the LOCKED state. Buying power = available cash (no
    # synthetic leverage) — this is what keeps the shared ledger from going
    # negative under contention; it applies equally to opening a long and to
    # buying a short back. Sells may open a short when ALLOW_SHORTING is set.
    if side == OrderSide.BUY:
        required = notional + commission
        if portfolio.cash_balance < required:
            return _reject(
                f"insufficient funds: need {required}, have {portfolio.cash_balance}"
            )
    elif not settings.ALLOW_SHORTING:
        held = position.qty if position else Decimal("0")
        if held < qty:
            return _reject(f"insufficient position: hold {held}, tried to sell {qty}")

    # (3) Fill. Record order + trade.
    order = Order(
        portfolio_id=portfolio_id, user_id=user_id, asset_id=asset_id,
        side=side, order_type=OrderType.MARKET, qty=qty,
        status=OrderStatus.FILLED, idempotency_key=idempotency_key,
    )
    db.add(order)
    db.flush()  # need order.id for the trade FK

    trade = Trade(
        order_id=order.id, portfolio_id=portfolio_id, user_id=user_id,
        asset_id=asset_id, side=side, qty=qty, fill_price=price, commission=commission,
    )
    db.add(trade)
    db.flush()

    # Position + cash mutation. Positions are SIGNED: qty > 0 long, < 0 short.
    # Cash is direction-agnostic — a buy debits, a sell credits — so shorting
    # (a sell that opens/extends a negative position) credits the proceeds.
    if position is None:
        position = Position(portfolio_id=portfolio_id, asset_id=asset_id,
                            qty=Decimal("0"), avg_entry_price=Decimal("0"))
        db.add(position)

    if side == OrderSide.BUY:
        portfolio.cash_balance = portfolio.cash_balance - notional - commission
        entry_type = LedgerEntryType.TRADE_BUY
        cash_delta = -(notional + commission)
        signed = qty
    else:  # SELL
        portfolio.cash_balance = portfolio.cash_balance + notional - commission
        entry_type = LedgerEntryType.TRADE_SELL
        cash_delta = notional - commission
        signed = -qty

    old_qty = position.qty
    new_qty = old_qty + signed
    if old_qty == 0 or (old_qty > 0) == (signed > 0):
        # Opening, or adding in the same direction → weighted-average entry.
        position.avg_entry_price = (
            (abs(old_qty) * position.avg_entry_price + qty * price) / abs(new_qty)
        ).quantize(Decimal("0.00000001"))
    else:
        # Reducing / closing (and maybe flipping through zero). Book realized
        # P&L on the closed portion — a long books (price − entry), a short the
        # inverse — and any excess opens a fresh position at `price`.
        closed = min(abs(old_qty), qty)
        realized = ((price - position.avg_entry_price) if old_qty > 0
                    else (position.avg_entry_price - price)) * closed
        position.realized_pnl = position.realized_pnl + realized.quantize(_CENTS)
        if qty > abs(old_qty):
            position.avg_entry_price = price          # flipped: new side opens at price
        elif new_qty == 0:
            position.avg_entry_price = Decimal("0")
    position.qty = new_qty

    # Signed ledger entry with running balance (self-verifying audit trail).
    # Non-USD fills record the quote currency AND the rate used — the ledger
    # must be replayable without asking "what was the rate that day".
    note = (f"{side.value} {qty} @ {price}" if asset_ccy == "USD" else
            f"{side.value} {qty} @ {price} {asset_ccy} (USD{asset_ccy} {fx})")
    db.add(LedgerEntry(
        portfolio_id=portfolio_id, trade_id=trade.id, entry_type=entry_type,
        amount=cash_delta.quantize(_CENTS), balance_after=portfolio.cash_balance,
        note=note,
    ))

    # (3b) Bump version so clients can detect stale state after reconnect.
    portfolio.version = portfolio.version + 1

    order.filled_at = trade.executed_at

    # (3c) Transactional outbox: the fill event commits WITH the ledger entry,
    # so a crash between commit and the route's Redis publish can never lose
    # it — the beat relay re-publishes any row still unmarked. Built before
    # commit so it snapshots the exact post-fill state.
    result = ExecutionResult(order.id, OrderStatus.FILLED, None, price, qty,
                             portfolio.cash_balance, portfolio.version)
    from app.models import OutboxEvent, User
    from app.services.events import portfolio_channel
    event = result.to_event(portfolio_id, user_id, asset_id, side)
    # Attribution (E5c) baked into the stored payload so the relay's replay
    # carries the actor's name exactly like the fast path.
    event["username"] = db.scalar(select(User.username).where(User.id == user_id))
    outbox = OutboxEvent(channel=portfolio_channel(portfolio_id), payload=event)
    db.add(outbox)
    db.commit()

    result.outbox_id = outbox.id
    result.event = event
    return result
