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
