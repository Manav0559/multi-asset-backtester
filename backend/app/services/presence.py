"""Presence — who's online in a shared portfolio room.

Backed by a Redis sorted set per portfolio, scored by last-seen epoch. A member
is "online" if seen within PRESENCE_TTL_S. This gives free TTL expiry without
per-member key TTLs: reads prune stale entries first, so a client that vanishes
(crash, closed tab) drops out within one TTL window even though it never sent a
clean disconnect — no zombie presence.
"""
from __future__ import annotations

import time

from app.services.events import _client as _redis

PRESENCE_TTL_S = 30


def _key(portfolio_id) -> str:
    return f"presence:{portfolio_id}"


def mark_online(portfolio_id, user_id) -> None:
    """Record/refresh a member's heartbeat."""
    _redis().zadd(_key(portfolio_id), {str(user_id): time.time()})


def mark_offline(portfolio_id, user_id) -> None:
    _redis().zrem(_key(portfolio_id), str(user_id))


def online_members(portfolio_id, now: float | None = None) -> list[str]:
    """Current online user_ids, pruning anyone past the TTL first."""
    now = now or time.time()
    r = _redis()
    r.zremrangebyscore(_key(portfolio_id), 0, now - PRESENCE_TTL_S)
    return r.zrange(_key(portfolio_id), 0, -1)
