from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Integer, Numeric
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import Timeframe


class OhlcvBar(Base):
    """Historical + streamed OHLCV bars. TimescaleDB hypertable.

    Hypertable config (applied in the Alembic migration, not here):
      - time partitioning on `time`, chunk_time_interval = 1 day
        (sized for moderate ingestion, ~1-10M rows/day)
      - space partitioning: hash on `asset_id`, 4 partitions,
        for parallel I/O across a large concurrent ticker universe
      - native compression: segment_by (asset_id, timeframe),
        order_by time DESC, compress chunks older than 7 days

    The composite PK deliberately puts `time` LAST so the underlying
    btree serves (asset, timeframe) time-range scans optimally — including
    `ORDER BY time DESC` hot paths, since btrees scan backwards — and it
    includes both partitioning columns as TimescaleDB requires. No extra
    secondary index is needed on this table.
    """

    __tablename__ = "ohlcv_bars"

    asset_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True
    )
    timeframe: Mapped[Timeframe] = mapped_column(
        Enum(Timeframe, name="timeframe", values_callable=lambda e: [m.value for m in e]),
        primary_key=True,
    )
    time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)

    open: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    volume: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False, server_default="0")
    trade_count: Mapped[int | None] = mapped_column(BigInteger)
    vwap: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
