from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.models.chat import ChatMessage, ChatSession


@dataclass
class MemoryChatMessage:
    id: str
    role: str
    content: str
    created_at: datetime


@dataclass
class MemoryChatSession:
    id: str
    user_id: str
    title: str
    created_at: datetime
    updated_at: datetime
    messages: list[MemoryChatMessage] = field(default_factory=list)


_memory_lock = RLock()
_memory_sessions_by_user: dict[str, dict[str, MemoryChatSession]] = {}


def _use_persistent_storage() -> bool:
    return get_settings().uses_persistent_chat_history


def _get_user_sessions(user_id: str) -> dict[str, MemoryChatSession]:
    with _memory_lock:
        return _memory_sessions_by_user.setdefault(user_id, {})


async def list_sessions(db: AsyncSession, *, user_id: str) -> list[ChatSession | MemoryChatSession]:
    if not _use_persistent_storage():
        sessions = list(_get_user_sessions(user_id).values())
        return sorted(sessions, key=lambda item: item.updated_at, reverse=True)
    result = await db.execute(
        select(ChatSession)
        .where(ChatSession.user_id == user_id)
        .order_by(ChatSession.updated_at.desc())
    )
    return list(result.scalars().all())


async def list_recent_messages(
    db: AsyncSession,
    *,
    user_id: str,
    limit: int = 50,
) -> list[ChatMessage | MemoryChatMessage]:
    if not _use_persistent_storage():
        messages: list[MemoryChatMessage] = []
        for session in _get_user_sessions(user_id).values():
            messages.extend(session.messages)
        return sorted(messages, key=lambda item: item.created_at, reverse=True)[:limit]

    result = await db.execute(
        select(ChatMessage)
        .join(ChatSession, ChatMessage.session_id == ChatSession.id)
        .where(ChatSession.user_id == user_id)
        .order_by(ChatMessage.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_session(
    db: AsyncSession,
    *,
    user_id: str,
    session_id: str,
) -> ChatSession | MemoryChatSession | None:
    if not _use_persistent_storage():
        return _get_user_sessions(user_id).get(session_id)
    result = await db.execute(
        select(ChatSession)
        .options(selectinload(ChatSession.messages))
        .where(ChatSession.id == session_id, ChatSession.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def create_session(
    db: AsyncSession,
    *,
    user_id: str,
    title: str,
    session_id: str | None = None,
) -> ChatSession | MemoryChatSession:
    if not _use_persistent_storage():
        now = datetime.now(timezone.utc)
        session = MemoryChatSession(
            id=session_id or str(uuid.uuid4()),
            user_id=user_id,
            title=title,
            created_at=now,
            updated_at=now,
        )
        _get_user_sessions(user_id)[session.id] = session
        return session
    resolved_session_id = session_id or str(uuid.uuid4())
    session = ChatSession(id=resolved_session_id, user_id=user_id, title=title)
    db.add(session)
    try:
        await db.flush()
    except IntegrityError:
        # Parallel requests can attempt to create the same client-provided session id.
        await db.rollback()
        existing_result = await db.execute(
            select(ChatSession).where(
                ChatSession.id == resolved_session_id,
                ChatSession.user_id == user_id,
            )
        )
        existing_session = existing_result.scalar_one_or_none()
        if existing_session is not None:
            return existing_session
        raise
    await db.refresh(session)
    return session


async def add_message(
    db: AsyncSession,
    *,
    session: ChatSession | MemoryChatSession,
    role: str,
    content: str,
) -> ChatMessage | MemoryChatMessage:
    if not _use_persistent_storage():
        message = MemoryChatMessage(
            id=str(uuid.uuid4()),
            role=role,
            content=content,
            created_at=datetime.now(timezone.utc),
        )
        session.updated_at = datetime.now(timezone.utc)
        session.messages.append(message)
        return message
    message = ChatMessage(session_id=session.id, role=role, content=content)
    session.updated_at = datetime.now(timezone.utc)
    db.add(message)
    await db.flush()
    await db.refresh(message)
    return message


def reset_memory_store() -> None:
    with _memory_lock:
        _memory_sessions_by_user.clear()
