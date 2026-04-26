from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.main_agent import AgentReply, fallback_response
from app.core.database import get_db
from app.deps import get_current_user
from app.models.user import User
from app.schemas.chat import ChatRequest, ChatResponse, ChatSessionDetail, ChatSessionSummary
from app.services import agent_service, chat_store

router = APIRouter(prefix="/chat", tags=["chat"])
logger = logging.getLogger(__name__)


def _build_title(message: str) -> str:
    words = message.strip().split()
    return " ".join(words[:6]) if words else "New conversation"


@router.get("/sessions", response_model=list[ChatSessionSummary])
async def list_chat_sessions(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ChatSessionSummary]:
    return await chat_store.list_sessions(db, user_id=user.id)


@router.get("/sessions/{session_id}", response_model=ChatSessionDetail)
async def get_chat_session(
    session_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChatSessionDetail:
    session = await chat_store.get_session(db, user_id=user.id, session_id=session_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat session not found.")
    return session


@router.post("/message", response_model=ChatResponse)
async def create_chat_message(
    payload: ChatRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChatResponse:
    # Read user id once up front so error handling never touches expired ORM state.
    user_id = user.id
    try:
        session = None
        if payload.session_id:
            session = await chat_store.get_session(db, user_id=user_id, session_id=payload.session_id)
            if session is None:
                session = await chat_store.create_session(
                    db,
                    user_id=user_id,
                    title=_build_title(payload.message),
                    session_id=payload.session_id,
                )
        if session is None:
            session = await chat_store.create_session(db, user_id=user_id, title=_build_title(payload.message))

        user_message = await chat_store.add_message(db, session=session, role="user", content=payload.message.strip())
        session_with_messages = await chat_store.get_session(db, user_id=user_id, session_id=session.id)
        if session_with_messages is None:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Chat session unavailable.")

        history = [(item.role, item.content) for item in session_with_messages.messages[:-1]]
        try:
            agent_reply = await agent_service.generate_reply(
                db,
                user=user,
                message=user_message.content,
                history=history,
            )
        except Exception:
            # Double-safety fallback: chat should still reply even if agent service fails unexpectedly.
            logger.exception("Agent service failed inside chat endpoint for user_id=%s", user_id)
            agent_reply = AgentReply(
                content=fallback_response(user_message.content),
                provider="fallback",
                used_tools=[],
            )
        assistant_message = await chat_store.add_message(
            db,
            session=session_with_messages,
            role="assistant",
            content=agent_reply.content,
        )
        await db.commit()
        session_after_reply = await chat_store.get_session(db, user_id=user_id, session_id=session.id)
        if session_after_reply is None:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Chat session unavailable.")

        return ChatResponse(
            session=session_after_reply,
            reply=assistant_message,
            provider=agent_reply.provider,
            used_tools=agent_reply.used_tools,
        )
    except HTTPException:
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
        logger.exception("Could not process chat message for user_id=%s", user_id, exc_info=exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not process your message right now. Please try again.",
        ) from exc
