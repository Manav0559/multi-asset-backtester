"""WebSocket hub — the browser-facing real-time fan-out layer.

Bridges Redis pub/sub (fed by the stream adapters + the portfolio ledger)
to authenticated browser WebSocket connections, with per-connection
subscription filtering so a client only receives the channels it asked for.

Architecture / scaling contract:
  * Each FastAPI process runs ONE ConnectionManager with ONE Redis pubsub.
  * The pubsub psubscribes to broad patterns (tick:*, bar:*, depth:*,
    portfolio:*) ONCE at startup and the reader loop owns it exclusively.
    Per-client filtering is in-memory (never dynamic (p)subscribe — redis-py's
    async PubSub deadlocks if you subscribe while listen() reads).
  * Fan-out rides Redis, so the ingest process (or order handler) is decoupled
    from the process holding the browser socket — the web tier scales out
    without sticky sessions.

Slow-consumer safety (the backpressure fix):
  * Each socket has a `_Sender` with a bounded, CONFLATING outbound buffer.
    tick:/depth:/bar: frames are conflatable — only the LATEST per channel is
    kept, so a client on hotel wifi that can't keep up simply skips stale
    ticks instead of ballooning our memory.
  * portfolio: frames are MUST-DELIVER (a missed fill or chat message is a
    correctness bug), so they queue; if a client can't drain even those
    (bounded queue overflows) it is disconnected rather than served stale.

Client protocol (JSON frames):
  ->  {"action":"subscribe","channels":["depth:BINANCE:BTCUSDT"]}
  ->  {"action":"unsubscribe","channels":[...]}
  <-  {"type":"subscribed","channels":[...]}
  <-  {"type":"message","channel":"...","data":{...}}
  <-  {"type":"error","detail":"..."}
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import defaultdict, deque

from fastapi import WebSocket

from app.core.metrics import WS_CLIENTS, WS_CONFLATED, WS_OVERFLOW_DISCONNECTS
from app.streaming.bus import TickBus

logger = logging.getLogger("streaming.hub")

_PUBLIC_PREFIXES = ("tick:", "bar:", "depth:")
_PORTFOLIO_PREFIX = "portfolio:"
_HUB_PATTERNS = ("tick:*", "bar:*", "depth:*", "portfolio:*")
# Only the latest value per conflatable channel matters; a slow client drops
# intermediate ones. Everything else (portfolio:) is must-deliver.
# "_hb" is the hub's own heartbeat pseudo-channel — always conflatable (a slow
# client only ever needs the newest liveness proof).
_CONFLATABLE_PREFIXES = ("tick:", "depth:", "bar:", "_hb")
_MUST_DELIVER_MAX = 1000  # queued must-deliver frames before we drop the client


def _is_conflatable(channel: str) -> bool:
    return channel.startswith(_CONFLATABLE_PREFIXES)


class _Sender:
    """Per-connection conflating outbound buffer + writer task.

    offer() is called from the reader loop (never blocks on the socket). The
    writer drains must-deliver frames first, then the latest conflated frame
    per channel. Overflow of must-deliver marks the client for disconnect.
    """

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self._conflated: dict[str, str] = {}   # channel -> latest frame
        self._must: deque[str] = deque()
        self._event = asyncio.Event()
        self.closed = False
        self.overflowed = False

    def offer(self, channel: str, frame: str) -> None:
        if _is_conflatable(channel):
            if channel in self._conflated:     # an undelivered frame is replaced
                WS_CONFLATED.labels(channel.split(":", 1)[0]).inc()
            self._conflated[channel] = frame   # overwrite: keep only latest
        elif len(self._must) >= _MUST_DELIVER_MAX:
            if not self.overflowed:            # count the client once, not per frame
                WS_OVERFLOW_DISCONNECTS.inc()
            self.overflowed = True             # can't keep up with must-deliver
        else:
            self._must.append(frame)
        self._event.set()

    def pending_conflated(self) -> int:
        """Test hook: how many distinct conflated channels are buffered."""
        return len(self._conflated)

    async def run(self) -> None:
        try:
            while not self.closed:
                await self._event.wait()
                self._event.clear()
                if self.overflowed:
                    break
                batch = list(self._must)
                self._must.clear()
                batch.extend(self._conflated.values())
                self._conflated.clear()
                for frame in batch:
                    await self.ws.send_text(frame)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — dead socket ends the writer, not the hub
            pass
        finally:
            self.closed = True


class ConnectionManager:
    def __init__(self, bus: TickBus | None = None):
        self.bus = bus or TickBus()
        self._subscribers: dict[str, set[WebSocket]] = defaultdict(set)
        self._conn_channels: dict[WebSocket, set[str]] = defaultdict(set)
        self._senders: dict[WebSocket, _Sender] = {}
        self._sender_tasks: dict[WebSocket, asyncio.Task] = {}
        self._pubsub = None
        self._reader_task: asyncio.Task | None = None
        self._hb_task: asyncio.Task | None = None
        self.epoch: str = ""   # set at start(); new per hub incarnation
        self._lock = asyncio.Lock()

    # ---- lifecycle --------------------------------------------------------
    async def start(self) -> None:
        await self.bus.connect()
        self._pubsub = self.bus.redis.pubsub()
        await self._pubsub.psubscribe(*_HUB_PATTERNS)
        self._reader_task = asyncio.create_task(self._reader_loop(), name="hub-reader")
        # Epoch identifies THIS hub incarnation. A client that sees the epoch
        # change knows the hub restarted while its socket auto-reconnected —
        # anything it "knew" may be stale, so it must resync over REST. The
        # heartbeat doubles as silent-gap detection (missed beats => the link
        # is dead even if TCP hasn't noticed yet).
        self.epoch = uuid.uuid4().hex
        self._hb_task = asyncio.create_task(self._heartbeat_loop(), name="hub-hb")
        logger.info("connection manager started (patterns=%s, epoch=%s)",
                    _HUB_PATTERNS, self.epoch)

    async def stop(self) -> None:
        for task in (self._reader_task, self._hb_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if self._pubsub:
            await self._pubsub.aclose()
        await self.bus.close()

    async def _heartbeat_loop(self) -> None:
        from app.core.config import settings
        while True:
            frame = json.dumps({"type": "hb", "epoch": self.epoch,
                                "ts": time.time()})
            for sender in list(self._senders.values()):
                if not sender.closed:
                    sender.offer("_hb", frame)
            await asyncio.sleep(settings.HUB_HEARTBEAT_SECONDS)

    # ---- connection registration ------------------------------------------
    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        sender = _Sender(ws)
        self._senders[ws] = sender
        self._sender_tasks[ws] = asyncio.create_task(sender.run(), name="ws-sender")
        WS_CLIENTS.inc()

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            channels = self._conn_channels.pop(ws, set())
            for ch in channels:
                subs = self._subscribers.get(ch)
                if subs is None:
                    continue
                subs.discard(ws)
                if not subs:
                    self._subscribers.pop(ch, None)
        sender = self._senders.pop(ws, None)
        if sender:
            sender.closed = True
            WS_CLIENTS.dec()   # paired with connect(); pop guards double-dec
        task = self._sender_tasks.pop(ws, None)
        if task:
            task.cancel()

    # ---- subscribe / unsubscribe ------------------------------------------
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
            for ws in targets:
                sender = self._senders.get(ws)
                if sender is None or sender.closed:
                    continue
                sender.offer(channel, frame)   # never blocks on the socket


manager = ConnectionManager()
