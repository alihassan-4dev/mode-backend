from datetime import datetime

from pydantic import BaseModel, Field


class ChatMessageOut(BaseModel):
    id: str
    role: str
    content: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ChatSessionSummary(BaseModel):
    id: str
    title: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ChatSessionDetail(ChatSessionSummary):
    messages: list[ChatMessageOut]


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = None


class ChatResponse(BaseModel):
    session: ChatSessionDetail
    reply: ChatMessageOut
    provider: str
    used_tools: list[str] = Field(default_factory=list)
