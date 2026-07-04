import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MlTrial(Base):
    """One attempted ML backtest, logged so the Deflated Sharpe Ratio can use a
    REAL trial count (not a client-supplied guess). `research_key` groups
    attempts at the same question — family + asset — so N reflects how many
    variants were tried before one 'won'. This is the multiple-testing
    correction Bailey & López de Prado (2014) exist to enforce."""

    __tablename__ = "ml_trials"
    __table_args__ = (Index("ix_ml_trials_research_key", "research_key"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    research_key: Mapped[str] = mapped_column(String(128), nullable=False)
    params_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
