"""Drop the cash_balance >= 0 floor to allow margin (leverage).

With MAX_LEVERAGE > 1, buying power exceeds cash, so a leveraged buy legitimately
drives cash negative (borrowed against equity). The old `cash_non_negative` CHECK
can no longer be the double-spend backstop; that guarantee now lives in
execution._buying_power, enforced under the portfolio's SELECT ... FOR UPDATE
lock (which serialises concurrent orders on the same book). The `qty_positive`
check on orders is unrelated and stays.

Revision ID: 0011
Revises: 0010
"""
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

# Logical name — the metadata naming convention expands it to the real
# constraint id `ck_portfolios_cash_non_negative`.
_CONSTRAINT = "cash_non_negative"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "portfolios", type_="check")


def downgrade() -> None:
    # Re-adding requires every row to already satisfy it (no leveraged books).
    op.create_check_constraint(_CONSTRAINT, "portfolios", "cash_balance >= 0")
