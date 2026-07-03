"""Idempotency keys on orders — exactly-once fills under client retries.

A client generates a key at order-confirm time and reuses it on every retry
(including the transparent retry after a 401 token refresh). The unique
constraint is the DB-level backstop; the executor's pre-check inside the
portfolio row lock is the fast path. NULL keys are allowed and never dedupe
(Postgres treats NULLs as distinct), so pre-idempotency callers and internal
paths keep working unchanged.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-04
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("idempotency_key", sa.String(64), nullable=True))
    op.create_unique_constraint(
        "uq_orders_portfolio_idempotency", "orders", ["portfolio_id", "idempotency_key"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_orders_portfolio_idempotency", "orders", type_="unique")
    op.drop_column("orders", "idempotency_key")
