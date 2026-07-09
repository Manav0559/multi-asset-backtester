from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class OutboxEvent(Base):
    """Transactional outbox for ledger-mutating portfolio events.

    A fill's event row is written in the SAME transaction as the ledger entry,
    so a crash between DB commit and the Redis publish can never lose the
    event: the fast path publishes immediately after commit and marks
    `published_at`; a beat relay sweeps rows still NULL (i.e. the process died
    in that window) and re-publishes them. Delivery is therefore at-least-once
    — consumers are idempotent (clients reload state keyed by `version`, and
    events carry `order_id` for dedupe).

    Only ledger-mutating events route through here. Ephemeral traffic (chat,
    presence, typing, market data) stays on the direct pub/sub fast path —
    losing one of those on a crash is cosmetic, and the outbox's write
    amplification isn't worth it.
    """

    __tablename__ = "outbox_events"
    __table_args__ = (
        # The relay's scan: only unpublished rows, oldest first.
        Index("ix_outbox_unpublished", "id",
              postgresql_where="published_at IS NULL"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(120), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
