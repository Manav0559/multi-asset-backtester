"""WebSocket endpoint: authenticate, stamp the socket with the user's
portfolio memberships, then hand message handling to the ConnectionManager.

Auth over WS: browsers can't set Authorization headers on the WS handshake,
so the access token is passed as a `?token=` query param (standard pattern).
We validate it exactly like the REST dependency — same signature, same
type check — and close with policy-violation (4401) on failure.

On connect we resolve the user's portfolio_ids ONCE and stamp them on
ws.state, so the hub can authorize portfolio:{id} subscriptions without a
DB hit per subscribe.
"""
from __future__ import annotations

import json
import platform
import uuid

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from app.core.security import ACCESS, TokenError, decode_token
from app.db.session import SessionLocal
from app.models import PortfolioMember, User
from app.streaming.hub import manager

router = APIRouter()

WS_POLICY_VIOLATION = 1008  # RFC 6455 close code for auth/policy failures


def _authenticate(token: str) -> User | None:
    try:
        payload = decode_token(token, expected_type=ACCESS)
    except TokenError:
        return None
    with SessionLocal() as db:
        user = db.get(User, uuid.UUID(payload["sub"]))
        if user is None or not user.is_active:
            return None
        return user


def _portfolio_channels(user_id: uuid.UUID) -> set[str]:
    with SessionLocal() as db:
        ids = db.scalars(
            select(PortfolioMember.portfolio_id).where(PortfolioMember.user_id == user_id)
        ).all()
    return {f"portfolio:{pid}" for pid in ids}


@router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket, token: str = Query(...)):
    user = _authenticate(token)
    if user is None:
        await websocket.close(code=WS_POLICY_VIOLATION, reason="Invalid or missing token")
        return

    # Stamp memberships so the hub can authorize portfolio:{id} subscriptions.
    websocket.state.user_id = user.id
    channels = _portfolio_channels(user.id)
    websocket.state.portfolio_channels = channels
    portfolio_ids = [c.split(":", 1)[1] for c in channels]

    await manager.connect(websocket)
    # `node` = which hub replica holds this socket (scale-out proof + ops).
    await websocket.send_text(json.dumps({"type": "connected", "user": str(user.id),
                                          "node": platform.node()}))
    await _presence(portfolio_ids, user.id, online=True)  # I'm here — tell the rooms

    try:
        while True:
            raw = await websocket.receive_text()
            await _dispatch(websocket, raw, portfolio_ids, user.id, user.username)
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(websocket)
        await _presence(portfolio_ids, user.id, online=False)  # I left — update the rooms


async def _presence(portfolio_ids: list[str], user_id, *, online: bool) -> None:
    """Mark presence and broadcast the room's online set. Runs the sync
    (lock-guarded) presence ops off the event loop."""
    import asyncio

    from app.services.events import publish_portfolio_event
    from app.services.presence import mark_offline, mark_online, online_members

    def _work():
        for pid in portfolio_ids:
            (mark_online if online else mark_offline)(pid, user_id)
            publish_portfolio_event(pid, {"type": "presence", "portfolio_id": pid,
                                          "online": online_members(pid)})
    await asyncio.to_thread(_work)


async def _heartbeat(portfolio_ids: list[str], user_id) -> None:
    """Refresh presence TTL on client ping (no rebroadcast — TTL just extends)."""
    import asyncio

    from app.services.presence import mark_online
    await asyncio.to_thread(lambda: [mark_online(pid, user_id) for pid in portfolio_ids])


async def _typing(portfolio_id: str, user_id, username) -> None:
    """Broadcast a transient 'X is typing' ping to the room. Ephemeral (never
    stored), member-gated by the caller, and server-capped so a stuck key can't
    flood the room — the client debounces too."""
    import asyncio

    from app.services.events import fixed_window_allow, publish_portfolio_event

    def _work():
        if fixed_window_allow(f"typing:{portfolio_id}:{user_id}", 5, 3):
            publish_portfolio_event(portfolio_id, {
                "type": "typing", "portfolio_id": portfolio_id,
                "user_id": str(user_id), "username": username})
    await asyncio.to_thread(_work)


async def _dispatch(websocket: WebSocket, raw: str,
                    portfolio_ids: list[str] | None = None, user_id=None,
                    username=None) -> None:
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        await websocket.send_text(json.dumps({"type": "error", "detail": "invalid JSON"}))
        return

    action = msg.get("action")
    channels = msg.get("channels", [])
    if not isinstance(channels, list):
        await websocket.send_text(json.dumps({"type": "error", "detail": "channels must be a list"}))
        return

    if action == "subscribe":
        accepted = await manager.subscribe(websocket, channels)
        rejected = [c for c in channels if c not in accepted]
        await websocket.send_text(json.dumps({
            "type": "subscribed", "channels": accepted,
            **({"rejected": rejected} if rejected else {}),
        }))
    elif action == "unsubscribe":
        await manager.unsubscribe(websocket, channels)
        await websocket.send_text(json.dumps({"type": "unsubscribed", "channels": channels}))
    elif action == "ping":
        if portfolio_ids and user_id is not None:
            await _heartbeat(portfolio_ids, user_id)  # refresh presence TTL
        await websocket.send_text(json.dumps({"type": "pong"}))
    elif action == "typing":
        # Fire-and-forget; only for a room the socket actually belongs to.
        portfolio = msg.get("portfolio")
        if user_id is not None and portfolio and portfolio in (portfolio_ids or []):
            await _typing(portfolio, user_id, username)
    else:
        await websocket.send_text(json.dumps({"type": "error", "detail": f"unknown action: {action}"}))
