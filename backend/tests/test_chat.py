"""E5d — portfolio chat: member-only, rate-limited, soft-delete, paginated."""
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select

from app.core.security import create_access_token, hash_password
from app.db.session import SessionLocal
from app.models import ChatMessage, PortfolioMember, User
from app.models.enums import PortfolioRole

_PW = hash_password("s3cret-pass!")


def _user(tag):
    s = uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        u = User(email=f"cht_{tag}_{s}@x.com", username=f"cht_{tag}_{s}", hashed_password=_PW)
        db.add(u); db.commit(); db.refresh(u)
        return {"id": u.id, "h": {"Authorization": f"Bearer {create_access_token(u.id)}"}}


@pytest.fixture()
def chat_env(client):
    owner, member, outsider = _user("o"), _user("m"), _user("x")
    pid = client.post("/portfolios", headers=owner["h"],
                      json={"name": "chat fund", "initial_cash": "1000.00"}).json()["id"]
    with SessionLocal() as db:
        db.add(PortfolioMember(portfolio_id=uuid.UUID(pid), user_id=member["id"],
                               role=PortfolioRole.TRADER))
        db.commit()
    yield {"owner": owner, "member": member, "outsider": outsider, "pid": pid}
    with SessionLocal() as db:
        pu = uuid.UUID(pid)
        db.execute(delete(ChatMessage).where(ChatMessage.portfolio_id == pu))
        db.execute(delete(PortfolioMember).where(PortfolioMember.portfolio_id == pu))
        from app.models import LedgerEntry, Portfolio
        db.execute(delete(LedgerEntry).where(LedgerEntry.portfolio_id == pu))
        db.execute(delete(Portfolio).where(Portfolio.id == pu))
        db.execute(delete(User).where(User.id.in_(
            [owner["id"], member["id"], outsider["id"]])))
        db.commit()


def test_members_can_chat_outsiders_cannot(client, chat_env):
    pid = chat_env["pid"]
    r = client.post(f"/portfolios/{pid}/chat", headers=chat_env["owner"]["h"],
                    json={"body": "gm team"})
    assert r.status_code == 201 and r.json()["body"] == "gm team"
    assert client.post(f"/portfolios/{pid}/chat", headers=chat_env["member"]["h"],
                       json={"body": "gm"}).status_code == 201
    # Outsider: 404 on both send and history (probe-proof).
    assert client.post(f"/portfolios/{pid}/chat", headers=chat_env["outsider"]["h"],
                       json={"body": "let me in"}).status_code == 404
    assert client.get(f"/portfolios/{pid}/chat",
                      headers=chat_env["outsider"]["h"]).status_code == 404

    hist = client.get(f"/portfolios/{pid}/chat", headers=chat_env["member"]["h"]).json()
    assert len(hist["messages"]) == 2  # newest first


def test_rate_limit(client, chat_env):
    pid, h = chat_env["pid"], chat_env["owner"]["h"]
    codes = [client.post(f"/portfolios/{pid}/chat", headers=h,
                         json={"body": f"m{i}"}).status_code for i in range(12)]
    assert codes[:10] == [201] * 10
    assert 429 in codes[10:]  # 11th/12th within the 10s window are throttled


def test_soft_delete_tombstone(client, chat_env):
    pid = chat_env["pid"]
    mid = client.post(f"/portfolios/{pid}/chat", headers=chat_env["member"]["h"],
                      json={"body": "oops secret"}).json()["id"]
    # A different member cannot delete it.
    assert client.delete(f"/portfolios/{pid}/chat/{mid}",
                         headers=chat_env["owner"]["h"]).status_code == 403
    # Author can; body is blanked, tombstone flagged.
    r = client.delete(f"/portfolios/{pid}/chat/{mid}", headers=chat_env["member"]["h"])
    assert r.status_code == 200 and r.json()["deleted"] is True and r.json()["body"] == ""
    hist = client.get(f"/portfolios/{pid}/chat", headers=chat_env["member"]["h"]).json()
    got = next(m for m in hist["messages"] if m["id"] == mid)
    assert got["deleted"] is True and got["body"] == ""


def test_cursor_pagination(client, chat_env):
    pid, h = chat_env["pid"], chat_env["member"]["h"]
    # 3 messages directly in DB (bypass rate limit) with distinct timestamps.
    from datetime import timedelta
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    with SessionLocal() as db:
        for i in range(3):
            db.add(ChatMessage(portfolio_id=uuid.UUID(pid), user_id=chat_env["member"]["id"],
                               body=f"msg{i}", created_at=base + timedelta(minutes=i)))
        db.commit()
    # before is strict (<): the cursor at msg2's exact time excludes it.
    page1 = client.get(f"/portfolios/{pid}/chat?before=2025-01-01T00:02:00Z", headers=h).json()
    bodies = [m["body"] for m in page1["messages"]]
    assert bodies == ["msg1", "msg0"]  # newest-first, strictly before the cursor
