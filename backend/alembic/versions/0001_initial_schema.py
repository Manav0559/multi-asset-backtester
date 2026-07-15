"""Initial schema: users, assets, portfolios (shared ledger), trading,
strategies, backtests, and the ohlcv_bars TimescaleDB hypertable.

Hypertable design decisions (financial tick/bar data best practices):
  * chunk_time_interval = 1 day  -> sized for moderate ingestion
    (~1-10M rows/day). Revisit to 1 hour only if ingestion exceeds
    ~100M rows/day.
  * Composite PK (asset_id, timeframe, time) puts the timestamp LAST,
    so the btree serves (ticker, time-range) scans optimally.
  * Space partitioning: hash(asset_id) across 4 partitions alongside
    time partitioning, for parallel I/O across a wide ticker universe.
  * Native columnar compression segmented by (asset_id, timeframe),
    ordered time DESC, auto-compressing chunks older than 7 days.

Revision ID: 0001
Revises:
Create Date: 2026-07-02

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ---------------------------------------------------------------------------
# Enum types: created explicitly once, referenced with create_type=False so
# reuse across tables (e.g. order_side on orders AND trades) can't collide.
# ---------------------------------------------------------------------------
asset_class = postgresql.ENUM(
    "crypto", "in_equity", "in_index", "us_equity", "commodity",
    name="asset_class", create_type=False,
)
timeframe = postgresql.ENUM(
    "1m", "5m", "15m", "1h", "1d", name="timeframe", create_type=False,
)
portfolio_role = postgresql.ENUM(
    "owner", "trader", "viewer", name="portfolio_role", create_type=False,
)
invite_status = postgresql.ENUM(
    "pending", "accepted", "declined", "expired", name="invite_status", create_type=False,
)
order_side = postgresql.ENUM("buy", "sell", name="order_side", create_type=False)
order_type = postgresql.ENUM("market", "limit", name="order_type", create_type=False)
order_status = postgresql.ENUM(
    "pending", "filled", "rejected", "cancelled", name="order_status", create_type=False,
)
ledger_entry_type = postgresql.ENUM(
    "deposit", "trade_buy", "trade_sell", "commission", "adjustment",
    name="ledger_entry_type", create_type=False,
)
backtest_status = postgresql.ENUM(
    "queued", "running", "completed", "failed", name="backtest_status", create_type=False,
)

ALL_ENUMS = [
    asset_class, timeframe, portfolio_role, invite_status, order_side,
    order_type, order_status, ledger_entry_type, backtest_status,
]


def _timescale_available(bind) -> bool:
    """True only when FULL (TSL-licensed) TimescaleDB is present — the path this
    migration takes creates a hypertable AND columnar compression, and the
    compression DDL is TSL-only.

    Availability is NOT sufficient: managed Postgres (Neon/Supabase) ships the
    Apache-2 build. It lists timescaledb in pg_available_extensions and will even
    create a hypertable, but `ALTER TABLE ... SET (timescaledb.compress ...)`
    errors under the 'apache' license and aborts the migration. So gate on the
    license GUC — the preloaded library reports 'timescale' only on a full
    install. Vanilla Postgres (no timescaledb) and the Apache-2 build both report
    something other than 'timescale' here and take the plain-table path, which
    the app's SQL fully supports (it never calls a Timescale function)."""
    return bind.exec_driver_sql(
        "SELECT current_setting('timescaledb.license', true)"
    ).scalar() == "timescale"


def upgrade() -> None:
    bind = op.get_bind()
    has_timescale = _timescale_available(bind)

    # --- TimescaleDB extension (only where it's available) ---
    if has_timescale:
        op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")

    for enum_type in ALL_ENUMS:
        enum_type.create(bind, checkfirst=True)

    # ------------------------------------------------------------- users --
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index("ix_users_username", "users", ["username"], unique=True)

    # ------------------------------------------------------------ assets --
    op.create_table(
        "assets",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("exchange", sa.String(32), nullable=False),
        sa.Column("asset_class", asset_class, nullable=False),
        sa.Column("name", sa.String(255)),
        sa.Column("currency", sa.String(8), nullable=False, server_default="USD"),
        sa.Column("tick_size", sa.Numeric(18, 8)),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.UniqueConstraint("symbol", "exchange", name="uq_assets_symbol_exchange"),
    )
    op.create_index("ix_assets_symbol", "assets", ["symbol"])
    op.create_index("ix_assets_asset_class", "assets", ["asset_class"])

    # -------------------------------------------------------- portfolios --
    op.create_table(
        "portfolios",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("base_currency", sa.String(8), nullable=False, server_default="USD"),
        sa.Column("initial_cash", sa.Numeric(20, 2), nullable=False),
        sa.Column("cash_balance", sa.Numeric(20, 2), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_public", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        # DB-level backstop for the Shared Ledger Rule: cash can never go
        # negative even if application-level locking has a bug.
        sa.CheckConstraint("cash_balance >= 0", name="cash_non_negative"),
    )
    op.create_index("ix_portfolios_owner_id", "portfolios", ["owner_id"])

    op.create_table(
        "portfolio_members",
        sa.Column("portfolio_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("portfolios.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("role", portfolio_role, nullable=False, server_default="trader"),
        sa.Column("invited_by", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_portfolio_members_user_id", "portfolio_members", ["user_id"])

    op.create_table(
        "portfolio_invites",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("portfolio_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False),
        sa.Column("inviter_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("invitee_email", sa.String(255), nullable=False),
        sa.Column("role", portfolio_role, nullable=False, server_default="trader"),
        sa.Column("status", invite_status, nullable=False, server_default="pending"),
        sa.Column("token", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_portfolio_invites_portfolio_id", "portfolio_invites", ["portfolio_id"])
    op.create_index("ix_portfolio_invites_invitee_email", "portfolio_invites", ["invitee_email"])
    op.create_index("ix_portfolio_invites_status", "portfolio_invites", ["status"])

    # ----------------------------------------------------------- trading --
    op.create_table(
        "orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("portfolio_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("asset_id", sa.Integer(),
                  sa.ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("side", order_side, nullable=False),
        sa.Column("order_type", order_type, nullable=False, server_default="market"),
        sa.Column("qty", sa.Numeric(28, 10), nullable=False),
        sa.Column("limit_price", sa.Numeric(20, 8)),
        sa.Column("status", order_status, nullable=False, server_default="pending"),
        sa.Column("reject_reason", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("filled_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("qty > 0", name="qty_positive"),
    )
    op.create_index("ix_orders_portfolio_created", "orders", ["portfolio_id", "created_at"])
    op.create_index("ix_orders_user_id", "orders", ["user_id"])
    op.create_index("ix_orders_asset_id", "orders", ["asset_id"])
    op.create_index("ix_orders_status", "orders", ["status"])

    op.create_table(
        "trades",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("order_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("portfolio_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("asset_id", sa.Integer(),
                  sa.ForeignKey("assets.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("side", order_side, nullable=False),
        sa.Column("qty", sa.Numeric(28, 10), nullable=False),
        sa.Column("fill_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("commission", sa.Numeric(20, 8), nullable=False, server_default="0"),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_trades_portfolio_executed", "trades", ["portfolio_id", "executed_at"])
    op.create_index("ix_trades_order_id", "trades", ["order_id"])

    op.create_table(
        "positions",
        sa.Column("portfolio_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("portfolios.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("asset_id", sa.Integer(),
                  sa.ForeignKey("assets.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("qty", sa.Numeric(28, 10), nullable=False, server_default="0"),
        sa.Column("avg_entry_price", sa.Numeric(20, 8), nullable=False, server_default="0"),
        sa.Column("realized_pnl", sa.Numeric(20, 2), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )

    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("portfolio_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False),
        sa.Column("trade_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("trades.id", ondelete="SET NULL")),
        sa.Column("entry_type", ledger_entry_type, nullable=False),
        sa.Column("amount", sa.Numeric(20, 2), nullable=False),
        sa.Column("balance_after", sa.Numeric(20, 2), nullable=False),
        sa.Column("note", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_ledger_entries_portfolio_created", "ledger_entries",
                    ["portfolio_id", "created_at"])

    # -------------------------------------------------------- strategies --
    op.create_table(
        "strategies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.UniqueConstraint("user_id", "name", name="uq_strategies_user_name"),
    )
    op.create_index("ix_strategies_user_id", "strategies", ["user_id"])

    op.create_table(
        "strategy_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("strategy_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("params", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.UniqueConstraint("strategy_id", "version",
                            name="uq_strategy_versions_strategy_version"),
    )
    op.create_index("ix_strategy_versions_strategy_id", "strategy_versions", ["strategy_id"])

    # --------------------------------------------------------- backtests --
    op.create_table(
        "backtests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("strategy_version_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("strategy_versions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("status", backtest_status, nullable=False, server_default="queued"),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("total_return_pct", sa.Numeric(12, 4)),
        sa.Column("cagr_pct", sa.Numeric(12, 4)),
        sa.Column("sharpe", sa.Numeric(10, 4)),
        sa.Column("sortino", sa.Numeric(10, 4)),
        sa.Column("deflated_sharpe", sa.Numeric(10, 4)),
        sa.Column("max_drawdown_pct", sa.Numeric(12, 4)),
        sa.Column("trade_count", sa.Integer()),
        sa.Column("win_rate_pct", sa.Numeric(7, 4)),
        sa.Column("equity_curve", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_backtests_user_created", "backtests", ["user_id", "created_at"])
    op.create_index("ix_backtests_strategy_version_id", "backtests", ["strategy_version_id"])
    op.create_index("ix_backtests_status", "backtests", ["status"])

    op.create_table(
        "backtest_yearly_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("backtest_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("backtests.id", ondelete="CASCADE"), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("return_pct", sa.Numeric(12, 4)),
        sa.Column("max_drawdown_pct", sa.Numeric(12, 4)),
        sa.Column("sharpe", sa.Numeric(10, 4)),
        sa.Column("sortino", sa.Numeric(10, 4)),
        sa.Column("volatility_pct", sa.Numeric(12, 4)),
        sa.Column("trade_count", sa.Integer()),
        sa.Column("win_rate_pct", sa.Numeric(7, 4)),
        sa.UniqueConstraint("backtest_id", "year",
                            name="uq_backtest_yearly_results_backtest_year"),
    )
    op.create_index("ix_backtest_yearly_results_backtest_id",
                    "backtest_yearly_results", ["backtest_id"])

    # ------------------------------------------- ohlcv_bars (hypertable) --
    op.create_table(
        "ohlcv_bars",
        sa.Column("asset_id", sa.Integer(),
                  sa.ForeignKey("assets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("timeframe", timeframe, nullable=False),
        sa.Column("time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(20, 8), nullable=False),
        sa.Column("high", sa.Numeric(20, 8), nullable=False),
        sa.Column("low", sa.Numeric(20, 8), nullable=False),
        sa.Column("close", sa.Numeric(20, 8), nullable=False),
        sa.Column("volume", sa.Numeric(28, 10), nullable=False, server_default="0"),
        sa.Column("trade_count", sa.BigInteger()),
        sa.Column("vwap", sa.Numeric(20, 8)),
        # Timestamp column LAST: the PK btree then serves
        # (ticker, timeframe, time-range) scans — the dominant query shape —
        # in both ASC and DESC directions. Includes both partitioning
        # columns (time + asset_id), as TimescaleDB requires.
        sa.PrimaryKeyConstraint("asset_id", "timeframe", "time", name="pk_ohlcv_bars"),
    )

    # Timescale-specific storage. On vanilla Postgres ohlcv_bars stays an
    # ordinary table indexed by the composite PK — correct, just without
    # hypertable chunking or columnar compression. The application queries
    # never call a Timescale function, so nothing downstream cares.
    if has_timescale:
        op.execute(
            """
            SELECT create_hypertable(
                'ohlcv_bars', 'time',
                partitioning_column   => 'asset_id',
                number_partitions     => 4,
                chunk_time_interval   => INTERVAL '1 day',
                create_default_indexes => FALSE
            )
            """
        )
        op.execute(
            """
            ALTER TABLE ohlcv_bars SET (
                timescaledb.compress,
                timescaledb.compress_segmentby = 'asset_id, timeframe',
                timescaledb.compress_orderby   = 'time DESC'
            )
            """
        )
        op.execute("SELECT add_compression_policy('ohlcv_bars', INTERVAL '7 days')")


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_table("ohlcv_bars")  # drops hypertable + compression policy
    op.drop_table("backtest_yearly_results")
    op.drop_table("backtests")
    op.drop_table("strategy_versions")
    op.drop_table("strategies")
    op.drop_table("ledger_entries")
    op.drop_table("positions")
    op.drop_table("trades")
    op.drop_table("orders")
    op.drop_table("portfolio_invites")
    op.drop_table("portfolio_members")
    op.drop_table("portfolios")
    op.drop_table("assets")
    op.drop_table("users")

    for enum_type in reversed(ALL_ENUMS):
        enum_type.drop(bind, checkfirst=True)
