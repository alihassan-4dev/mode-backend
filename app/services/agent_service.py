from __future__ import annotations

import asyncio
import logging

from app.agent.main_agent import AgentReply, fallback_response, run_agent
from app.core.config import get_settings

logger = logging.getLogger(__name__)


async def generate_reply(*args, **kwargs) -> AgentReply:
    try:
        settings = get_settings()
        timeout_seconds = max(5.0, float(settings.AGENT_TIMEOUT_SECONDS))
        return await asyncio.wait_for(run_agent(*args, **kwargs), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        message = ""
        if "message" in kwargs and isinstance(kwargs["message"], str):
            message = kwargs["message"]
        elif len(args) >= 4 and isinstance(args[3], str):
            message = args[3]
        logger.warning("Agent run timed out, returning fallback reply")
        return AgentReply(
            content=(
                "I am taking too long to generate a detailed answer right now. "
                "Here is a quick response while I recover:\n\n"
                f"{fallback_response(message)}"
            ),
            provider="fallback-timeout",
            used_tools=[],
        )
    except Exception:
        message = ""
        if "message" in kwargs and isinstance(kwargs["message"], str):
            message = kwargs["message"]
        elif len(args) >= 4 and isinstance(args[3], str):
            # Signature: run_agent(db, *, user, message, history)
            message = args[3]
        logger.exception("Agent run failed, returning safe fallback reply")
        return AgentReply(
            content=fallback_response(message),
            provider="fallback",
            used_tools=[],
        )
