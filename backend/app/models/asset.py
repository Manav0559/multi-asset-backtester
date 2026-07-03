from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import AssetClass


class Asset(Base):
    """Instrument master. One row per tradeable symbol per exchange.

    `id` is a compact integer FK target for the high-volume ohlcv_bars
    hypertable (int joins/compression beat string symbols at scale).
    """

    __tablename__ = "assets"
    __table_args__ = (UniqueConstraint("symbol", "exchange", name="uq_assets_symbol_exchange"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)   # e.g. BTCUSDT, RELIANCE, AAPL
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)             # BINANCE, NSE, NASDAQ, NYSE, MCX
    asset_class: Mapped[AssetClass] = mapped_column(
        Enum(AssetClass, name="asset_class", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        index=True,
    )
    name: Mapped[str | None] = mapped_column(String(255))
    currency: Mapped[str] = mapped_column(String(8), nullable=False, server_default="USD")
    tick_size: Mapped[float | None] = mapped_column(Numeric(18, 8))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
