"""Import every model so Base.metadata sees the full schema
(required for Alembic autogenerate diffs)."""
from app.db.base import Base
from app.models.asset import Asset
from app.models.backtest import Backtest, BacktestYearlyResult
from app.models.challenge import Challenge
from app.models.chat import ChatMessage
from app.models.market_data import OhlcvBar
from app.models.ml_trial import MlTrial
from app.models.outbox import OutboxEvent
from app.models.portfolio import Portfolio, PortfolioInvite, PortfolioMember
from app.models.snapshot import PortfolioEquitySnapshot
from app.models.strategy import Strategy, StrategyVersion
from app.models.trading import LedgerEntry, Order, Position, Trade
from app.models.user import User

__all__ = [
    "Base",
    "Asset",
    "Backtest",
    "BacktestYearlyResult",
    "Challenge",
    "ChatMessage",
    "OhlcvBar",
    "OutboxEvent",
    "Portfolio",
    "PortfolioEquitySnapshot",
    "PortfolioInvite",
    "PortfolioMember",
    "Strategy",
    "StrategyVersion",
    "LedgerEntry",
    "MlTrial",
    "Order",
    "Position",
    "Trade",
    "User",
]
