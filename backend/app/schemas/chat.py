import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class ChatSend(BaseModel):
    body: str = Field(min_length=1, max_length=2000)


class ChatMessageOut(BaseModel):
    id: uuid.UUID
    portfolio_id: uuid.UUID
    user_id: uuid.UUID
    username: str
    body: str            # blanked to "" when deleted
    deleted: bool
    created_at: datetime


class ChatPage(BaseModel):
    """Cursor-paginated history, newest-first. `next_cursor` is the created_at
    of the oldest message returned; pass it as `before` to page back."""
    messages: list[ChatMessageOut]
    next_cursor: datetime | None = None
