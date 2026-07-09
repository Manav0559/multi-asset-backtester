"""Transactional outbox for ledger-mutating portfolio events.

Closes the dual-write hole: a fill's DB commit and its Redis publish were two
unrelated operations, so a crash between them silently lost the event on every
collaborator's screen. The outbox row commits WITH the ledger entry; the fast
path publishes right after commit and stamps published_at; a beat relay
re-publishes anything left NULL. At-least-once by construction.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("channel", sa.String(120), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
    )
    # Partial index: the relay only ever scans unpublished rows.
    op.create_index("ix_outbox_unpublished", "outbox_events", ["id"],
                    postgresql_where=sa.text("published_at IS NULL"))


def downgrade() -> None:
    op.drop_table("outbox_events")
