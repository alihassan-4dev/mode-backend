from __future__ import annotations

import logging

from app.agent.main_agent import AgentReply, fallback_response, run_agent

logger = logging.getLogger(__name__)


async def generate_reply(*args, **kwargs) -> AgentReply:
    try:
        return await run_agent(*args, **kwargs)
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
