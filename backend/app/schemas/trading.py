import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import OrderSide


class OrderCreate(BaseModel):
    asset_id: int
    side: OrderSide
    qty: Decimal = Field(gt=0)
    # Client-generated at confirm time, reused on every retry of the same
    # intent (incl. the transparent retry after a 401 refresh). Optional:
    # omitting it keeps the old non-idempotent behavior.
    idempotency_key: str | None = Field(default=None, max_length=64)


class OrderResult(BaseModel):
    order_id: uuid.UUID
    status: str
    reason: str | None = None
    fill_price: Decimal | None = None
    filled_qty: Decimal | None = None
    cash_balance: Decimal
    version: int


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    asset_id: int
    side: str
    status: str
    qty: Decimal
    reject_reason: str | None
    created_at: datetime
    filled_at: datetime | None
