"""Synchronous Redis publisher for portfolio ledger events.

Order execution is a synchronous SELECT-FOR-UPDATE transaction, so we
publish its resulting event with a sync Redis client (not the async bus).
Events go to portfolio:{id}; the WS hub relays them to every collaborator
connected to that channel — this is what makes User B's screen update the
instant User A trades.

CRITICAL: callers must publish only AFTER the DB transaction commits, so a
rolled-back trade can never surface on a teammate's screen.
"""
from __future__ import annotations

import json
import uuid
from functools import lru_cache

import redis

from app.core.config import settings


@lru_cache(maxsize=1)
def _client() -> redis.Redis:
    return redis.from_url(settings.REDIS_URL, decode_responses=True)


def portfolio_channel(portfolio_id: uuid.UUID) -> str:
    return f"portfolio:{portfolio_id}"


def publish_portfolio_event(portfolio_id: uuid.UUID, event: dict) -> None:
    _client().publish(portfolio_channel(portfolio_id), json.dumps(event))


def fixed_window_allow(key: str, limit: int, window_s: int) -> bool:
    """Redis fixed-window rate limit. True if the action is allowed. Fails OPEN
    if Redis is unreachable (availability over strictness for a trading
    simulator; the hard invariants live in Postgres)."""
    try:
        c = _client()
        n = c.incr(key)
        if n == 1:
            c.expire(key, window_s)
        return n <= limit
    except redis.RedisError:
        return True


# ---------------------------------------------------------------- outbox --
def mark_outbox_published(outbox_id: int) -> None:
    """Fast-path bookkeeping after a successful publish. Its own tiny
    transaction; if THIS write is lost to a crash the relay re-publishes the
    event — at-least-once, and consumers dedupe by order_id/version."""
    from sqlalchemy import func, update

    from app.db.session import SessionLocal
    from app.models import OutboxEvent

    with SessionLocal() as db:
        db.execute(update(OutboxEvent).where(OutboxEvent.id == outbox_id)
                   .values(published_at=func.now()))
        db.commit()


def relay_outbox(limit: int = 500, retain_days: int = 7) -> dict:
    """Sweep unpublished outbox rows (a process died between DB commit and its
    Redis publish) and re-publish them in id order. SKIP LOCKED so concurrent
    relays never double-publish a row. Also prunes published rows older than
    `retain_days` so the table can't grow without bound."""
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
        now = datetime.now(timezone.utc)  # a real datetime, NOT func.now():
        # the prune DELETE below synchronizes the session by EVALUATING its
        # WHERE clause against in-memory rows, and a SQL clause can't be
        # boolean-compared in Python.
        for row in rows:
            _client().publish(row.channel, json.dumps(row.payload))
            row.published_at = now
            published += 1
        cutoff = datetime.now(timezone.utc) - timedelta(days=retain_days)
        pruned = db.execute(
            delete(OutboxEvent).where(OutboxEvent.published_at.isnot(None),
                                      OutboxEvent.published_at < cutoff)
        ).rowcount
        db.commit()
    return {"published": published, "pruned": pruned}
