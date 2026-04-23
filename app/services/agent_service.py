from __future__ import annotations

from app.agent.main_agent import AgentReply, run_agent


async def generate_reply(*args, **kwargs) -> AgentReply:
    return await run_agent(*args, **kwargs)
