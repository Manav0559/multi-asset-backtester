"""FX rates for multi-currency correctness.

NSE assets quote in rupees but portfolio cash is USD; before this, an NSE fill
at Rs.2500 debited $2500 — a unit bug in the ledger. Fills, valuations, and
leaderboard equity now convert through this table.
"""
import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fx_rates",
        sa.Column("pair", sa.String(12), primary_key=True),
        sa.Column("rate", sa.Numeric(20, 8), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("fx_rates")
