"""Re-chunk ohlcv_bars: monthly chunks, no hash space-partitioning.

0001 sized the hypertable for streaming ingestion (1-day chunks, 4-way hash
space partitioning on asset_id). The actual workload is a 2020->now historical
BACKFILL plus a trickle of live bars, so that layout exploded into ~6,900
chunks of ~43 rows each. Once the 7-day compression policy had covered nearly
all of them, query PLANNING — which opens per-chunk columnstore metadata —
took 30+ seconds for the leaderboard's latest-close correlated subqueries
(execution itself was fine). Rebuild with:

  * chunk_time_interval = 1 month  -> ~80 chunks for the same data
  * no space partitioning          -> hash partitioning buys parallel I/O on
    multi-disk clusters; on this single-volume deploy it only multiplied the
    chunk count by 4 (Timescale's own docs recommend against it here)
  * same compression settings + 7-day policy (the active monthly chunk stays
    uncompressed, so live-bar inserts and latest-close reads keep the fast path)

The table is tiny (~300K rows), so copy-rebuild is the honest, simple move.
Downgrade restores the 0001 layout (slow: it recreates thousands of chunks).
"""
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

_COLUMNS_DDL = """
CREATE TABLE ohlcv_bars (
    asset_id    INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    timeframe   timeframe NOT NULL,
    time        TIMESTAMPTZ NOT NULL,
    open        NUMERIC(20, 8) NOT NULL,
    high        NUMERIC(20, 8) NOT NULL,
    low         NUMERIC(20, 8) NOT NULL,
    close       NUMERIC(20, 8) NOT NULL,
    volume      NUMERIC(28, 10) NOT NULL DEFAULT 0,
    trade_count BIGINT,
    vwap        NUMERIC(20, 8),
    CONSTRAINT pk_ohlcv_bars PRIMARY KEY (asset_id, timeframe, time)
)
"""

_COMPRESSION_DDL = """
ALTER TABLE ohlcv_bars SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'asset_id, timeframe',
    timescaledb.compress_orderby   = 'time DESC'
)
"""


def _rebuild(hypertable_sql: str) -> None:
    # Move the old table aside. The PK's backing index lives in the
    # schema-wide index namespace, so it must be renamed too; the FK name is
    # per-table and can stay.
    op.execute("ALTER TABLE ohlcv_bars RENAME TO ohlcv_bars_old")
    op.execute("ALTER TABLE ohlcv_bars_old "
               "RENAME CONSTRAINT pk_ohlcv_bars TO pk_ohlcv_bars_old")

    op.execute(_COLUMNS_DDL)
    op.execute(hypertable_sql)
    op.execute(_COMPRESSION_DDL)

    # Reading compressed chunks has no decompression-limit (that guard applies
    # to UPDATE/DELETE on compressed data), so a straight copy is safe.
    op.execute("INSERT INTO ohlcv_bars SELECT * FROM ohlcv_bars_old")
    op.execute("DROP TABLE ohlcv_bars_old")  # drops its compression policy too
    op.execute("SELECT add_compression_policy('ohlcv_bars', INTERVAL '7 days')")


def upgrade() -> None:
    _rebuild(
        """
        SELECT create_hypertable(
            'ohlcv_bars', 'time',
            chunk_time_interval    => INTERVAL '1 month',
            create_default_indexes => FALSE
        )
        """
    )


def downgrade() -> None:
    _rebuild(
        """
        SELECT create_hypertable(
            'ohlcv_bars', 'time',
            partitioning_column    => 'asset_id',
            number_partitions      => 4,
            chunk_time_interval    => INTERVAL '1 day',
            create_default_indexes => FALSE
        )
        """
    )
