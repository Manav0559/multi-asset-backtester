"""Consent-based head-to-head competitions.

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-04
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STATUS = ("pending", "active", "declined", "cancelled", "finished")


def upgrade() -> None:
    challenge_status = postgresql.ENUM(*_STATUS, name="challenge_status")
    challenge_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "challenges",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status",
                  postgresql.ENUM(*_STATUS, name="challenge_status", create_type=False),
                  server_default="pending", nullable=False),
        sa.Column("challenger_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("challenger_portfolio_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("opponent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("opponent_portfolio_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("duration_days", sa.Integer(), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("challenger_baseline", sa.Numeric(20, 2), nullable=True),
        sa.Column("opponent_baseline", sa.Numeric(20, 2), nullable=True),
        sa.Column("final_metrics", postgresql.JSONB(), nullable=True),
        sa.Column("winner_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("challenger_id <> opponent_id", name="challenge_distinct_users"),
        sa.ForeignKeyConstraint(["challenger_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["opponent_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["winner_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["challenger_portfolio_id"], ["portfolios.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["opponent_portfolio_id"], ["portfolios.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_challenges_challenger", "challenges", ["challenger_id"])
    op.create_index("ix_challenges_opponent", "challenges", ["opponent_id"])
    op.create_index("ix_challenges_status", "challenges", ["status"])
    op.create_index("ix_challenges_end_at", "challenges", ["end_at"])


def downgrade() -> None:
    op.drop_table("challenges")
    postgresql.ENUM(name="challenge_status").drop(op.get_bind(), checkfirst=True)
