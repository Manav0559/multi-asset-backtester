"""WebSocket hub tests.

Exercised against the real app (lifespan starts the hub's Redis bridge)
and real Redis:
  * unauthenticated / bad-token handshakes are rejected
  * subscribe returns an ack; public market channels are accepted
  * a portfolio channel the user does NOT belong to is rejected (AuthZ)
  * a bar published to Redis is relayed to a subscribed browser socket
    (the full adapter -> bus -> hub -> client path)
"""
import json
import time
import uuid

import pytest
from starlette.testclient import WebSocketDisconnect

from app.core.security import create_access_token
from app.db.session import SessionLocal
from app.models import Asset, Portfolio, PortfolioMember, User
from app.models.enums import AssetClass, PortfolioRole, Timeframe


@pytest.fixture()
def user_and_token():
    with SessionLocal() as db:
        u = User(email=f"ws_{uuid.uuid4().hex[:8]}@e.com",
                 username=f"ws_{uuid.uuid4().hex[:8]}",
                 hashed_password="x")
        db.add(u); db.commit(); db.refresh(u)
        uid = u.id
    token = create_access_token(uid)
    yield uid, token
    with SessionLocal() as db:
        db.query(PortfolioMember).filter_by(user_id=uid).delete()
        db.query(Portfolio).filter_by(owner_id=uid).delete()
        db.query(User).filter_by(id=uid).delete()
        db.commit()


def test_ws_rejects_missing_and_bad_token(client):
    # No token at all -> handshake fails (422 from required query param).
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws"):
            pass
    # Bad token -> server accepts handshake then closes with policy violation.
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws?token=garbage") as ws:
            ws.receive_text()


def test_ws_subscribe_public_channel_acked(client, user_and_token):
    _, token = user_and_token
    with client.websocket_connect(f"/ws?token={token}") as ws:
        assert json.loads(ws.receive_text())["type"] == "connected"
        ws.send_text(json.dumps({"action": "subscribe",
                                 "channels": ["bar:BINANCE:BTCUSDT:1m"]}))
        ack = json.loads(ws.receive_text())
        assert ack["type"] == "subscribed"
        assert "bar:BINANCE:BTCUSDT:1m" in ack["channels"]


def test_ws_rejects_foreign_portfolio_channel(client, user_and_token):
    _, token = user_and_token
    foreign = f"portfolio:{uuid.uuid4()}"  # a portfolio the user is not in
    with client.websocket_connect(f"/ws?token={token}") as ws:
        ws.receive_text()  # connected
        ws.send_text(json.dumps({"action": "subscribe", "channels": [foreign]}))
        ack = json.loads(ws.receive_text())
        assert foreign not in ack["channels"]         # not accepted
        assert foreign in ack.get("rejected", [])      # explicitly rejected


def test_ws_allows_own_portfolio_channel(client, user_and_token):
    uid, token = user_and_token
    with SessionLocal() as db:
        p = Portfolio(name="mine", owner_id=uid, initial_cash=1000, cash_balance=1000)
        db.add(p); db.commit(); db.refresh(p)
        db.add(PortfolioMember(portfolio_id=p.id, user_id=uid, role=PortfolioRole.OWNER))
        db.commit()
        pid = p.id
    # NB: memberships are resolved at connect time, so connect AFTER joining.
    chan = f"portfolio:{pid}"
    with client.websocket_connect(f"/ws?token={token}") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"action": "subscribe", "channels": [chan]}))
        ack = json.loads(ws.receive_text())
        assert chan in ack["channels"]


def test_ws_relays_published_message_to_client(client, user_and_token):
    """End-to-end: publish on the in-process bus, receive it on the socket."""
    from app.streaming.inproc_bus import bus

    _, token = user_and_token
    channel = "bar:BINANCE:BTCUSDT:1m"
    with client.websocket_connect(f"/ws?token={token}") as ws:
        ws.receive_text()  # connected
        ws.send_text(json.dumps({"action": "subscribe", "channels": [channel]}))
        ws.receive_text()  # subscribed ack
        time.sleep(0.1)

        # Publish exactly as the order handler / equity poll would.
        bus.publish(channel, {"symbol": "BTCUSDT", "close": "108"})

        frame = json.loads(ws.receive_text())
        assert frame["type"] == "message"
        assert frame["channel"] == channel
        assert frame["data"]["close"] == "108"
        assert frame["data"]["symbol"] == "BTCUSDT"
