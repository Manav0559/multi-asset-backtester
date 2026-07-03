"""Periodic per-portfolio equity snapshots (written by Celery beat) so the
leaderboard can rank on time-windowed returns (24h / 7d) in pure SQL.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-02

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "portfolio_equity_snapshots",
        sa.Column("portfolio_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cash", sa.Numeric(20, 2), nullable=False),
        sa.Column("equity", sa.Numeric(20, 2), nullable=False),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"], ondelete="CASCADE"),
        # (portfolio_id, time) ordering makes the PK btree serve the windowed
        # baseline lookup (latest snapshot <= cutoff per portfolio) directly.
        sa.PrimaryKeyConstraint("portfolio_id", "time"),
    )


def downgrade() -> None:
    op.drop_table("portfolio_equity_snapshots")
