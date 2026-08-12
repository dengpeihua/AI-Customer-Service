import datetime as dt
from pydantic import BaseModel


class ConversationSummary(BaseModel):
    id: int
    contact_id: str
    channel: str
    status: str
    last_text: str
    last_at: dt.datetime
    message_count: int


class MessageOut(BaseModel):
    id: int
    direction: str
    sender: str
    provenance: str | None = None
    delivery_status: str | None = None
    text: str
    created_at: dt.datetime


class ConversationDetail(BaseModel):
    id: int
    contact_id: str
    channel: str
    status: str
    created_at: dt.datetime
    messages: list[MessageOut]
