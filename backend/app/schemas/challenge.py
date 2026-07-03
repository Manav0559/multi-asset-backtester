"""Challenge schemas. `OpponentMetricsOut` is the consent contract made
explicit: it is the ONLY shape of another participant's data that ever leaves
the server. A schema-snapshot test guards it so adding a field (e.g. leaking
positions) fails loudly.
"""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ChallengeCreate(BaseModel):
    opponent_username: str
    challenger_portfolio_id: uuid.UUID
    duration_days: int = Field(ge=1, le=365)


class ChallengeAccept(BaseModel):
    opponent_portfolio_id: uuid.UUID


class CurvePoint(BaseModel):
    t: str
    v: float


class OpponentMetricsOut(BaseModel):
    """Whitelisted aggregates about a participant — nothing else is exposed."""
    return_pct: float
    max_drawdown_pct: float
    sharpe: float
    win_rate: float
    n_trades: int
    equity: str
    curve: list[CurvePoint]


class ChallengeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: str
    challenger_id: uuid.UUID
    challenger_username: str
    opponent_id: uuid.UUID
    opponent_username: str
    duration_days: int
    start_at: datetime | None = None
    end_at: datetime | None = None
    winner_id: uuid.UUID | None = None
    created_at: datetime
    # Whether the current viewer is the challenger (UI convenience).
    viewer_is_challenger: bool


class HeadToHeadOut(BaseModel):
    """Live/finished comparison. `you` and `them` are both OpponentMetrics-shaped
    aggregates; raw data for neither side is included."""
    challenge: ChallengeOut
    you: OpponentMetricsOut
    them: OpponentMetricsOut
    frozen: bool  # True once finished — metrics no longer move
