"""Portfolio chat — members only, live over the existing portfolio:{id} WS room.

Delivery reuses publish_portfolio_event (type="chat") so collaborators already
subscribed to their portfolio channel receive messages with no new plumbing.
History is cursor-paginated. Sending is rate-limited (10 / 10s / user). No
edits; the author may soft-delete (tombstone).
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require_portfolio_role
from app.db.session import get_db
from app.models import ChatMessage, PortfolioMember, User
from app.models.enums import PortfolioRole
from app.schemas.chat import ChatMessageOut, ChatPage, ChatSend
from app.services.events import fixed_window_allow, publish_portfolio_event

router = APIRouter(prefix="/portfolios/{portfolio_id}/chat", tags=["chat"])

_PAGE = 50
_RATE_LIMIT = 10
_RATE_WINDOW_S = 10


def _to_out(msg: ChatMessage, username: str) -> ChatMessageOut:
    deleted = msg.deleted_at is not None
    return ChatMessageOut(
        id=msg.id, portfolio_id=msg.portfolio_id, user_id=msg.user_id,
        username=username, body="" if deleted else msg.body,
        deleted=deleted, created_at=msg.created_at,
    )


@router.get("", response_model=ChatPage)
def get_history(portfolio_id: uuid.UUID, before: datetime | None = Query(None),
                member: PortfolioMember = Depends(require_portfolio_role(PortfolioRole.VIEWER)),
                db: Session = Depends(get_db)) -> ChatPage:
    q = (select(ChatMessage, User.username)
         .join(User, User.id == ChatMessage.user_id)
         .where(ChatMessage.portfolio_id == portfolio_id))
    if before is not None:
        q = q.where(ChatMessage.created_at < before)
    rows = db.execute(q.order_by(ChatMessage.created_at.desc()).limit(_PAGE + 1)).all()
    has_more = len(rows) > _PAGE
    rows = rows[:_PAGE]
    msgs = [_to_out(m, uname) for m, uname in rows]
    next_cursor = rows[-1][0].created_at if (has_more and rows) else None
    return ChatPage(messages=msgs, next_cursor=next_cursor)


@router.post("", response_model=ChatMessageOut, status_code=status.HTTP_201_CREATED)
def send_message(portfolio_id: uuid.UUID, body: ChatSend,
                 member: PortfolioMember = Depends(require_portfolio_role(PortfolioRole.VIEWER)),
                 db: Session = Depends(get_db)) -> ChatMessageOut:
    if not fixed_window_allow(f"chat_rl:{portfolio_id}:{member.user_id}",
                              _RATE_LIMIT, _RATE_WINDOW_S):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            detail="slow down — 10 messages / 10s",
                            headers={"Retry-After": str(_RATE_WINDOW_S)})
    msg = ChatMessage(portfolio_id=portfolio_id, user_id=member.user_id, body=body.body)
    db.add(msg); db.commit(); db.refresh(msg)
    username = db.scalar(select(User.username).where(User.id == member.user_id))
    out = _to_out(msg, username)
    # Live fan-out over the existing portfolio room (after commit).
    publish_portfolio_event(portfolio_id, {
        "type": "chat", "portfolio_id": str(portfolio_id),
        "id": str(msg.id), "user_id": str(member.user_id), "username": username,
        "body": msg.body, "created_at": msg.created_at.isoformat(),
    })
    return out


@router.delete("/{message_id}", response_model=ChatMessageOut)
def delete_message(portfolio_id: uuid.UUID, message_id: uuid.UUID,
                   member: PortfolioMember = Depends(require_portfolio_role(PortfolioRole.VIEWER)),
                   db: Session = Depends(get_db)) -> ChatMessageOut:
    msg = db.get(ChatMessage, message_id)
    if msg is None or msg.portfolio_id != portfolio_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "message not found")
    if msg.user_id != member.user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "you can only delete your own messages")
    if msg.deleted_at is None:
        msg.deleted_at = datetime.now(timezone.utc)
        db.commit(); db.refresh(msg)
    username = db.scalar(select(User.username).where(User.id == member.user_id))
    publish_portfolio_event(portfolio_id, {
        "type": "chat_deleted", "portfolio_id": str(portfolio_id), "id": str(msg.id)})
    return _to_out(msg, username)
