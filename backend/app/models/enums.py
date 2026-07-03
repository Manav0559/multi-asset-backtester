"""Shared enum types. Kept in one module so both models and Alembic
migrations reference a single source of truth."""
import enum


class AssetClass(str, enum.Enum):
    CRYPTO = "crypto"
    IN_EQUITY = "in_equity"      # NSE stocks
    IN_INDEX = "in_index"        # NIFTY 50, BANKNIFTY, ...
    US_EQUITY = "us_equity"      # NASDAQ / NYSE via Alpaca
    COMMODITY = "commodity"      # Gold, Silver, Crude, ...


class Timeframe(str, enum.Enum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    D1 = "1d"


class PortfolioRole(str, enum.Enum):
    OWNER = "owner"
    TRADER = "trader"
    VIEWER = "viewer"


class InviteStatus(str, enum.Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    EXPIRED = "expired"


class OrderSide(str, enum.Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, enum.Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(str, enum.Enum):
    PENDING = "pending"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class LedgerEntryType(str, enum.Enum):
    DEPOSIT = "deposit"          # initial funding / top-ups
    TRADE_BUY = "trade_buy"      # cash out
    TRADE_SELL = "trade_sell"    # cash in
    COMMISSION = "commission"
    ADJUSTMENT = "adjustment"    # admin corrections


class BacktestStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ChallengeStatus(str, enum.Enum):
    PENDING = "pending"      # proposed, opponent hasn't responded
    ACTIVE = "active"        # both consented, running to end_at
    DECLINED = "declined"    # opponent declined
    CANCELLED = "cancelled"  # challenger withdrew while pending
    FINISHED = "finished"    # ran to completion, metrics frozen
