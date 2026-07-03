"""Order placement + history — the trading half of Module C.

POST places a market order via the serialized SELECT-FOR-UPDATE executor,
then (only after commit) publishes the result to portfolio:{id} so every
connected collaborator's UI updates live. Requires >= trader role.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require_portfolio_role
from app.db.session import get_db
from app.models import Order, PortfolioMember
from app.models.enums import PortfolioRole
from app.schemas.trading import OrderCreate, OrderOut, OrderResult
from app.services.events import publish_portfolio_event
from app.services.execution import ExecutionError, execute_market_order

router = APIRouter(prefix="/portfolios/{portfolio_id}", tags=["orders"])


@router.post("/orders", response_model=OrderResult)
def place_order(portfolio_id: uuid.UUID, body: OrderCreate,
                member: PortfolioMember = Depends(require_portfolio_role(PortfolioRole.TRADER)),
                db: Session = Depends(get_db)) -> OrderResult:
    try:
        result = execute_market_order(
            db, portfolio_id=portfolio_id, user_id=member.user_id,
            asset_id=body.asset_id, side=body.side, qty=body.qty,
        )
    except ExecutionError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=str(exc)) from exc

    # Publish AFTER commit: a rolled-back trade never reaches a teammate's UI.
    publish_portfolio_event(
        portfolio_id,
        result.to_event(portfolio_id, member.user_id, body.asset_id, body.side),
    )
    return OrderResult(
        order_id=result.order_id, status=result.status.value, reason=result.reason,
        fill_price=result.fill_price, filled_qty=result.filled_qty,
        cash_balance=result.cash_balance, version=result.version,
    )


@router.get("/orders", response_model=list[OrderOut])
def list_orders(portfolio_id: uuid.UUID,
                member: PortfolioMember = Depends(require_portfolio_role(PortfolioRole.VIEWER)),
                db: Session = Depends(get_db)) -> list[Order]:
    return db.scalars(
        select(Order).where(Order.portfolio_id == portfolio_id)
        .order_by(Order.created_at.desc()).limit(200)
    ).all()
