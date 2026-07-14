"""Portfolio event publishing + a small in-memory rate limiter.

Single-process deployment: events go to the in-process bus (app/streaming/
inproc_bus), which fans them out to the WebSocket hub. This is what makes User
B's screen update the instant User A trades.

CRITICAL: callers must publish only AFTER the DB transaction commits, so a
rolled-back trade can never surface on a teammate's screen.
"""
from __future__ import annotations

import threading
import time
import uuid
from collections import defaultdict

from app.streaming.inproc_bus import bus


def portfolio_channel(portfolio_id: uuid.UUID) -> str:
    return f"portfolio:{portfolio_id}"


def publish_portfolio_event(portfolio_id: uuid.UUID, event: dict) -> None:
    bus.publish(portfolio_channel(portfolio_id), event)


# ---------------------------------------------------------- rate limit --
# Fixed-window counters in process memory. On one web process this is exact;
# it also fails OPEN (never raises) — availability over strictness for a paper
# venue, matching the old Redis behavior. Guarded by a lock (callers span the
# event loop + the sync threadpool).
_rl_lock = threading.Lock()
_rl_counts: dict[str, tuple[int, float]] = defaultdict(lambda: (0, 0.0))


def fixed_window_allow(key: str, limit: int, window_s: int) -> bool:
    """True if the action is allowed within the current fixed window."""
    now = time.time()
    with _rl_lock:
        count, window_start = _rl_counts[key]
        if now - window_start >= window_s:
            _rl_counts[key] = (1, now)
            return True
        _rl_counts[key] = (count + 1, window_start)
        return count + 1 <= limit


# ---------------------------------------------------------------- outbox --
def mark_outbox_published(outbox_id: int) -> None:
    """Bookkeeping after a successful publish. Its own tiny transaction; if this
    write is lost to a crash the relay re-publishes the event (at-least-once —
    consumers dedupe by order_id/version)."""
    from sqlalchemy import func, update

    from app.db.session import SessionLocal
    from app.models import OutboxEvent

    with SessionLocal() as db:
        db.execute(update(OutboxEvent).where(OutboxEvent.id == outbox_id)
                   .values(published_at=func.now()))
        db.commit()


def relay_outbox(limit: int = 500, retain_days: int = 7) -> dict:
    """Re-publish outbox rows whose fast-path publish never happened (a crash
    between DB commit and the in-process publish), then prune old published
    rows. Run periodically by the scheduler."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import delete, select

    from app.db.session import SessionLocal
    from app.models import OutboxEvent

    published = 0
    with SessionLocal() as db:
        rows = db.execute(
            select(OutboxEvent).where(OutboxEvent.published_at.is_(None))
            .order_by(OutboxEvent.id).limit(limit)
            .with_for_update(skip_locked=True)
        ).scalars().all()
        now = datetime.now(timezone.utc)
        for row in rows:
            bus.publish(row.channel, row.payload)
            row.published_at = now
            published += 1
        cutoff = datetime.now(timezone.utc) - timedelta(days=retain_days)
        pruned = db.execute(
            delete(OutboxEvent).where(OutboxEvent.published_at.isnot(None),
                                      OutboxEvent.published_at < cutoff)
        ).rowcount
        db.commit()
    return {"published": published, "pruned": pruned}
