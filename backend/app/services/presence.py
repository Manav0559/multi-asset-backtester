"""Presence — who's online in a shared portfolio room.

In-process, single-writer-friendly: a dict of {portfolio_id: {user_id:
last_seen}} guarded by a lock. Reads prune anyone past the TTL first, so a
client that vanishes (crash, closed tab) drops out within one TTL window even
without a clean disconnect — no zombie presence. (In single-process showcase
mode this is exact; it also resets on a cold start, which is fine.)
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict

PRESENCE_TTL_S = 30

_lock = threading.Lock()
_rooms: dict[str, dict[str, float]] = defaultdict(dict)


def mark_online(portfolio_id, user_id) -> None:
    with _lock:
        _rooms[str(portfolio_id)][str(user_id)] = time.time()


def mark_offline(portfolio_id, user_id) -> None:
    with _lock:
        _rooms[str(portfolio_id)].pop(str(user_id), None)


def online_members(portfolio_id, now: float | None = None) -> list[str]:
    """Current online user_ids, pruning anyone past the TTL first."""
    now = now or time.time()
    key = str(portfolio_id)
    with _lock:
        room = _rooms.get(key)
        if not room:
            return []
        stale = [u for u, seen in room.items() if seen < now - PRESENCE_TTL_S]
        for u in stale:
            del room[u]
        return list(room.keys())
