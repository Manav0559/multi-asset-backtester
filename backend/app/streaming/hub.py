"""WebSocket hub — the browser-facing real-time fan-out layer.

Bridges Redis pub/sub (fed by the stream adapters + the portfolio ledger)
to authenticated browser WebSocket connections, with per-connection
subscription filtering so a client only receives the channels it asked for.

Architecture / scaling contract:
  * Each FastAPI process runs ONE ConnectionManager with ONE Redis pubsub.
  * The pubsub psubscribes to broad patterns (tick:*, bar:*, portfolio:*)
    ONCE at startup and the reader loop owns it exclusively. Per-client
    filtering is done in-memory (the _subscribers map), never via dynamic
    (p)subscribe calls — redis-py's async PubSub is single-connection and
    calling psubscribe() while listen() is reading deadlocks them. Broad
    startup patterns sidestep that entirely.
  * Trade-off: a process receives every market channel from Redis even if
    only some are wanted locally, then filters. Fine at our scale; if a
    single process ever needs to relay only a slice, shard by running
    dedicated hubs per pattern — no code change to the filtering path.
  * Because fan-out rides Redis, the process that ingests a vendor stream
    (or handles an order) is fully decoupled from the process holding the
    browser socket. This is what lets the web tier scale horizontally.

Channel names reuse the Step-3 scheme (tick:{exch}:{sym},
bar:{exch}:{sym}:{tf}) plus portfolio:{uuid} for ledger events (Step 5).

Client protocol (JSON frames):
  ->  {"action":"subscribe","channels":["bar:BINANCE:BTCUSDT:1m"]}
  ->  {"action":"unsubscribe","channels":[...]}
  <-  {"type":"subscribed","channels":[...]}       (ack)
  <-  {"type":"message","channel":"...","data":{...}}   (relayed payload)
  <-  {"type":"error","detail":"..."}
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict

from fastapi import WebSocket

from app.streaming.bus import TickBus

logger = logging.getLogger("streaming.hub")

# Channel prefixes a client is allowed to subscribe to directly.
_PUBLIC_PREFIXES = ("tick:", "bar:")
_PORTFOLIO_PREFIX = "portfolio:"

# Broad patterns the reader owns from startup; covers every channel the
# adapters (Step 3) and the ledger (Step 5) publish to.
_HUB_PATTERNS = ("tick:*", "bar:*", "portfolio:*")


class ConnectionManager:
    """Owns all local WS connections + the single shared Redis subscription,
    and routes relayed messages to the right sockets."""

    def __init__(self, bus: TickBus | None = None):
        self.bus = bus or TickBus()
        # channel -> set of sockets locally subscribed to it
        self._subscribers: dict[str, set[WebSocket]] = defaultdict(set)
        # socket -> channels it's subscribed to (for O(1) disconnect cleanup)
        self._conn_channels: dict[WebSocket, set[str]] = defaultdict(set)
        self._pubsub = None
        self._reader_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # ---- lifecycle (called from FastAPI lifespan) -------------------------
    async def start(self) -> None:
        await self.bus.connect()
        self._pubsub = self.bus.redis.pubsub()
        # Subscribe to broad patterns ONCE; the reader owns the pubsub after this.
        await self._pubsub.psubscribe(*_HUB_PATTERNS)
        self._reader_task = asyncio.create_task(self._reader_loop(), name="hub-reader")
        logger.info("connection manager started (patterns=%s)", _HUB_PATTERNS)

    async def stop(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._pubsub:
            await self._pubsub.aclose()
        await self.bus.close()

    # ---- connection registration ------------------------------------------
    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()

    async def disconnect(self, ws: WebSocket) -> None:
        """Remove a socket from all in-memory routing maps. No Redis calls:
        the pubsub keeps its broad startup patterns for the process
        lifetime, so a disconnect is pure local bookkeeping."""
        async with self._lock:
            channels = self._conn_channels.pop(ws, set())
            for ch in channels:
                subs = self._subscribers.get(ch)
                if subs is None:
                    continue
                subs.discard(ws)
                if not subs:
                    self._subscribers.pop(ch, None)

    # ---- subscribe / unsubscribe (in-memory routing only) -----------------
    async def subscribe(self, ws: WebSocket, channels: list[str]) -> list[str]:
        accepted: list[str] = []
        async with self._lock:
            for ch in channels:
                if not self._is_allowed(ws, ch):
                    continue
                self._subscribers[ch].add(ws)
                self._conn_channels[ws].add(ch)
                accepted.append(ch)
        return accepted

    async def unsubscribe(self, ws: WebSocket, channels: list[str]) -> None:
        async with self._lock:
            for ch in channels:
                self._conn_channels[ws].discard(ch)
                subs = self._subscribers.get(ch)
                if subs is None:
                    continue
                subs.discard(ws)
                if not subs:
                    self._subscribers.pop(ch, None)

    def _is_allowed(self, ws: WebSocket, channel: str) -> bool:
        """Authorization gate for a subscription request. Public market
        channels are open to any authenticated user; portfolio channels
        require membership, checked at subscribe time against the set the
        endpoint stamped on the socket."""
        if channel.startswith(_PUBLIC_PREFIXES):
            return True
        if channel.startswith(_PORTFOLIO_PREFIX):
            allowed = getattr(ws.state, "portfolio_channels", set())
            return channel in allowed
        return False

    # ---- Redis -> browser relay -------------------------------------------
    async def _reader_loop(self) -> None:
        assert self._pubsub is not None
        async for message in self._pubsub.listen():
            if message["type"] != "pmessage":
                continue
            channel = message["channel"]
            targets = list(self._subscribers.get(channel, ()))
            if not targets:
                continue
            frame = json.dumps({
                "type": "message",
                "channel": channel,
                "data": json.loads(message["data"]),
            })
            await asyncio.gather(
                *(self._safe_send(ws, frame) for ws in targets),
                return_exceptions=True,
            )

    @staticmethod
    async def _safe_send(ws: WebSocket, frame: str) -> None:
        try:
            await ws.send_text(frame)
        except Exception:  # noqa: BLE001 — a dead socket shouldn't break the relay
            pass


# Process-wide singleton, wired into FastAPI lifespan in main.py.
manager = ConnectionManager()
