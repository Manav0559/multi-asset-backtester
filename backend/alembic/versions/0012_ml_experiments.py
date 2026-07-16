"""ml_experiments: minimal experiment tracking for ML backtests.

One row per completed ML run — model family, the hyperparameters actually used
(tuned or default), and the outcome metrics (OOS accuracy, Brier, Sharpe,
Deflated Sharpe). Complements ml_trials, which only counts attempts for the
DSR multiple-testing correction.

Revision ID: 0012
Revises: 0011
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ml_experiments",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("backtest_id", UUID(as_uuid=True), nullable=True),
        sa.Column("model_id", sa.String(64), nullable=False),
        sa.Column("params", JSONB, nullable=False, server_default="{}"),
        sa.Column("oos_accuracy", sa.Numeric(8, 4), nullable=True),
        sa.Column("brier_score", sa.Numeric(8, 4), nullable=True),
        sa.Column("sharpe", sa.Numeric(12, 4), nullable=True),
        sa.Column("deflated_sharpe", sa.Numeric(12, 4), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_ml_experiments_model_id", "ml_experiments", ["model_id"])


def downgrade() -> None:
    op.drop_table("ml_experiments")
