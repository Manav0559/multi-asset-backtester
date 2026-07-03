import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.enums import PortfolioRole


class PortfolioCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    initial_cash: Decimal = Field(gt=0)
    base_currency: str = Field(default="USD", max_length=8)
    is_public: bool = False


class PortfolioOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    owner_id: uuid.UUID
    base_currency: str
    initial_cash: Decimal
    cash_balance: Decimal
    version: int
    is_public: bool
    created_at: datetime


class MemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: uuid.UUID
    role: PortfolioRole
    joined_at: datetime


class InviteCreate(BaseModel):
    invitee_email: EmailStr
    role: PortfolioRole = PortfolioRole.TRADER


class InviteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    portfolio_id: uuid.UUID
    invitee_email: EmailStr
    role: PortfolioRole
    status: str
    token: str
    expires_at: datetime


class AcceptInvite(BaseModel):
    token: str


class PortfolioRename(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class PendingInviteOut(BaseModel):
    """An invite the current user can act on, enriched for the bell menu."""
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    portfolio_id: uuid.UUID
    portfolio_name: str
    inviter_username: str
    role: PortfolioRole
    token: str
    expires_at: datetime


class PositionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    asset_id: int
    qty: Decimal
    avg_entry_price: Decimal
    realized_pnl: Decimal


class EquityPointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    time: datetime
    cash: Decimal
    equity: Decimal


class LedgerEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    entry_type: str
    amount: Decimal
    balance_after: Decimal
    note: str | None
    created_at: datetime
