"""Add a diagnostics JSONB column to backtests for ML-specific outputs
(out-of-sample accuracy, feature importance, etc.).

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-02

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("backtests", sa.Column("diagnostics", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("backtests", "diagnostics")
