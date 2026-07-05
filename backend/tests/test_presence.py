"""E5e — presence: who's online in a shared portfolio room.

Three proofs:
  * the Redis sorted-set prunes zombies on read (a heartbeat older than the TTL
    drops out even without a clean disconnect);
  * the WS lifecycle marks a member online on connect and offline on disconnect,
    and the REST endpoint resolves the online set to usernames (for avatars);
  * connecting a teammate broadcasts the updated online set to everyone already
    subscribed to the portfolio room (the live avatar update).

Runs against the real app (lifespan starts the hub's Redis relay) + real Redis.
"""
import json
import time
import uuid

import pytest
from sqlalchemy import delete

from app.core.security import create_access_token, hash_password
from app.db.session import SessionLocal
from app.models import LedgerEntry, Portfolio, PortfolioMember, User
from app.models.enums import PortfolioRole
from app.services.events import _client
from app.services.presence import (
    PRESENCE_TTL_S,
    mark_online,
    online_members,
)

_PW = hash_password("s3cret-pass!")


def _user(tag):
    s = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        u = User(email=f"pres_{tag}_{s}@x.com", username=f"pres_{tag}_{s}",
                 hashed_password=_PW)
        db.add(u); db.commit(); db.refresh(u)
        return {"id": u.id, "username": u.username, "token": create_access_token(u.id),
                "h": {"Authorization": f"Bearer {create_access_token(u.id)}"}}


@pytest.fixture()
def pres_env(client):
    owner, member = _user("o"), _user("m")
    pid = client.post("/portfolios", headers=owner["h"],
                      json={"name": "presence fund", "initial_cash": "1000.00"}).json()["id"]
    with SessionLocal() as db:
        db.add(PortfolioMember(portfolio_id=uuid.UUID(pid), user_id=member["id"],
                               role=PortfolioRole.TRADER))
        db.commit()
    _client().delete(f"presence:{pid}")  # start from a clean room
    yield {"owner": owner, "member": member, "pid": pid}
    _client().delete(f"presence:{pid}")
    with SessionLocal() as db:
        pu = uuid.UUID(pid)
        db.execute(delete(PortfolioMember).where(PortfolioMember.portfolio_id == pu))
        db.execute(delete(LedgerEntry).where(LedgerEntry.portfolio_id == pu))
        db.execute(delete(Portfolio).where(Portfolio.id == pu))
        db.execute(delete(User).where(User.id.in_([owner["id"], member["id"]])))
        db.commit()


def _presence_ids(client, pid, h):
    return {o["user_id"] for o in client.get(f"/portfolios/{pid}/presence", headers=h).json()}


def _wait_presence(client, pid, h, expect, tries=150):
    """Poll the room until it matches `expect`. Disconnect runs async in the
    server task's finally block (mark_offline off the event loop), so on a
    thrashing box we must not assert on a single read — each GET also pumps the
    test portal, giving the finally a chance to land."""
    got = _presence_ids(client, pid, h)
    for _ in range(tries):
        if got == expect:
            return got
        time.sleep(0.1)
        got = _presence_ids(client, pid, h)
    return got


def test_presence_ttl_prunes_zombies():
    pid = uuid.uuid4()
    fresh, stale = uuid.uuid4(), uuid.uuid4()
    r, key = _client(), f"presence:{pid}"
    r.delete(key)
    try:
        mark_online(pid, fresh)
        # A member who crashed: last heartbeat older than the TTL window, never
        # cleanly removed. It must not linger.
        r.zadd(key, {str(stale): time.time() - PRESENCE_TTL_S - 5})
        online = online_members(pid)
        assert str(fresh) in online
        assert str(stale) not in online  # pruned on read
    finally:
        r.delete(key)


def test_ws_connect_marks_online_with_usernames(client, pres_env):
    pid, owner, member = pres_env["pid"], pres_env["owner"], pres_env["member"]

    assert client.get(f"/portfolios/{pid}/presence", headers=owner["h"]).json() == []

    with client.websocket_connect(f"/ws?token={owner['token']}") as wso:
        wso.receive_text()  # connected
        online = client.get(f"/portfolios/{pid}/presence", headers=owner["h"]).json()
        assert {o["user_id"] for o in online} == {str(owner["id"])}
        # username resolved for the avatar
        assert online[0]["username"] == owner["username"]

        # A second member connecting joins the room's online set.
        with client.websocket_connect(f"/ws?token={member['token']}") as wsm:
            wsm.receive_text()  # connected
            both = _wait_presence(client, pid, owner["h"],
                                  {str(owner["id"]), str(member["id"])})
            assert both == {str(owner["id"]), str(member["id"])}


def test_ws_disconnect_removes_from_room(client, pres_env):
    """A clean tab-close drops the member from the room (mark_offline in the
    server task's finally). Isolated to one top-level socket so the assertion
    runs only after the session has fully torn down."""
    pid, owner = pres_env["pid"], pres_env["owner"]

    with client.websocket_connect(f"/ws?token={owner['token']}") as wso:
        wso.receive_text()  # connected
        assert _presence_ids(client, pid, owner["h"]) == {str(owner["id"])}

    # socket closed -> room empties (may lag on a loaded box; poll)
    assert _wait_presence(client, pid, owner["h"], set()) == set()


def test_presence_broadcast_to_subscribed_teammate(client, pres_env):
    pid, owner, member = pres_env["pid"], pres_env["owner"], pres_env["member"]
    chan = f"portfolio:{pid}"

    with client.websocket_connect(f"/ws?token={owner['token']}") as wso:
        wso.receive_text()  # connected (own-connect presence fires before we subscribe)
        wso.send_text(json.dumps({"action": "subscribe", "channels": [chan]}))
        assert json.loads(wso.receive_text())["type"] == "subscribed"
        time.sleep(0.2)

        # A teammate joining broadcasts the new online set to the room. The
        # owner's OWN connect-presence ([owner] only) can be relayed late (async
        # reader-loop hop lands after we subscribed), so read presence frames
        # until we see the guaranteed teammate-join broadcast listing both.
        with client.websocket_connect(f"/ws?token={member['token']}") as wsm:
            wsm.receive_text()  # connected
            target = {str(owner["id"]), str(member["id"])}
            seen = []
            for _ in range(6):
                frame = json.loads(wso.receive_text())
                if frame.get("channel") == chan and frame["data"].get("type") == "presence":
                    online = set(frame["data"]["online"])
                    seen.append(online)
                    if online == target:
                        break
            assert target in seen, f"never saw both online; got {seen}"


def test_typing_ping_broadcasts_to_room(client, pres_env):
    """A member typing broadcasts an ephemeral 'X is typing' with their username
    to everyone in the room. Resolved at connect (no per-ping DB hit)."""
    pid, owner, member = pres_env["pid"], pres_env["owner"], pres_env["member"]
    chan = f"portfolio:{pid}"

    with client.websocket_connect(f"/ws?token={owner['token']}") as wso:
        wso.receive_text()  # connected
        wso.send_text(json.dumps({"action": "subscribe", "channels": [chan]}))
        assert json.loads(wso.receive_text())["type"] == "subscribed"
        time.sleep(0.2)

        with client.websocket_connect(f"/ws?token={member['token']}") as wsm:
            wsm.receive_text()  # connected
            wsm.send_text(json.dumps({"action": "typing", "portfolio": pid}))
            # skip presence frames from the joins; assert the typing broadcast
            typing = None
            for _ in range(8):
                frame = json.loads(wso.receive_text())
                if frame.get("channel") == chan and frame["data"].get("type") == "typing":
                    typing = frame["data"]
                    break
            assert typing is not None, "never received a typing frame"
            assert typing["user_id"] == str(member["id"])
            assert typing["username"] == member["username"]
