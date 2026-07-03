import uuid
from decimal import Decimal

from pydantic import BaseModel


class LeaderboardEntryOut(BaseModel):
    rank: int
    portfolio_id: uuid.UUID
    name: str
    members: list[str]
    initial_cash: Decimal
    equity: Decimal
    return_pct: Decimal
    spark: list[Decimal]  # downsampled equity curve for the row sparkline
