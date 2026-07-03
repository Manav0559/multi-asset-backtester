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
    websocket.state.portfolio_channels = _portfolio_channels(user.id)

    await manager.connect(websocket)
    await websocket.send_text(json.dumps({"type": "connected", "user": str(user.id)}))

    try:
        while True:
            raw = await websocket.receive_text()
            await _dispatch(websocket, raw)
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(websocket)


async def _dispatch(websocket: WebSocket, raw: str) -> None:
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
        await websocket.send_text(json.dumps({"type": "pong"}))
    else:
        await websocket.send_text(json.dumps({"type": "error", "detail": f"unknown action: {action}"}))
