"""In-process pub/sub — the single-process replacement for Redis fan-out.

The whole reason the platform used Redis pub/sub was to decouple THREE
processes (web, Celery worker, ticker). In free-tier "showcase" mode everything
runs in ONE FastAPI process, so a module-level asyncio bus does the same job
with no external broker.

The one subtlety: publishers live in two worlds. WebSocket handlers run on the
event loop; order execution runs in FastAPI's sync threadpool. So `publish()`
must be callable from any thread and marshal the delivery back onto the loop —
that is exactly what `loop.call_soon_threadsafe` is for. Delivery itself (the
fan-out to subscribed sockets) always runs on the loop thread, so the hub's
data structures need no locking beyond what it already has.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

logger = logging.getLogger("streaming.inproc_bus")


class InProcessBus:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._deliver: Callable[[str, dict], None] | None = None

    def bind(self, loop: asyncio.AbstractEventLoop,
             deliver: Callable[[str, dict], None]) -> None:
        """Wire the running loop + the hub's on-loop delivery callback. Called
        once at app startup."""
        self._loop = loop
        self._deliver = deliver

    def unbind(self) -> None:
        self._loop = None
        self._deliver = None

    def publish(self, channel: str, data: dict) -> None:
        """Fan `data` out to every socket subscribed to `channel`. Safe to call
        from any thread; a no-op if the bus isn't bound (e.g. a unit test with
        no running app), so publishers never need to care whether anyone is
        listening."""
        loop, deliver = self._loop, self._deliver
        if loop is None or deliver is None:
            return
        try:
            loop.call_soon_threadsafe(deliver, channel, data)
        except RuntimeError:  # loop closed mid-shutdown — drop
            pass


bus = InProcessBus()
