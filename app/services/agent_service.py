from __future__ import annotations

from dataclasses import dataclass

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.user import User
from app.services import dashboard as dashboard_service
from app.services import token_store


class PlatformLookupInput(BaseModel):
    platform: str = Field(description="facebook or instagram")


class WellnessSummaryInput(BaseModel):
    include_recommendations: bool = Field(description="Whether to include next-step recommendations")


@dataclass
class AgentReply:
    content: str
    provider: str
    used_tools: list[str]


def _build_system_prompt(user: User) -> str:
    settings = get_settings()
    name = user.full_name or user.email.split("@")[0]
    return (
        f"You are {settings.AGENT_SYSTEM_NAME}, a calm and practical wellness support assistant. "
        f"The current user is {name}. Keep answers human, direct, and supportive. "
        "Do not diagnose medical conditions. Use available tools when the user asks about dashboard "
        "status, connected platforms, or account data. Prefer short paragraphs and concrete next steps."
    )


async def _build_tools(db: AsyncSession, user: User):
    @tool("get_connected_platform", args_schema=PlatformLookupInput)
    async def get_connected_platform(platform: str) -> str:
        """Look up whether a social platform is connected and when it was last synced."""
        connection = await token_store.get_connection(db, user_id=user.id, platform=platform.lower())
        if not connection:
            return f"{platform} is not connected."
        account_name = connection.platform_name or connection.platform_username or connection.platform_user_id
        synced_at = connection.last_synced_at.isoformat() if connection.last_synced_at else "never"
        return f"{platform} is connected as {account_name}. Last synced at {synced_at}."

    @tool("get_wellness_summary", args_schema=WellnessSummaryInput)
    async def get_wellness_summary(include_recommendations: bool) -> str:
        """Return the latest dashboard summary and recommendations."""
        summary = await dashboard_service.build_dashboard_summary(db, user)
        lines = [f"{metric.label}: {metric.display_value} ({metric.detail})" for metric in summary.metrics]
        if include_recommendations:
            lines.extend(summary.recommendations)
        return "\n".join(lines)

    return [get_connected_platform, get_wellness_summary]


def _fallback_response(message: str) -> str:
    lower = message.lower()
    if "stress" in lower or "anx" in lower:
        return (
            "Your message sounds stress-related. Start with one small reset: pause, breathe slowly for "
            "60 seconds, and decide on the next single action instead of the whole day."
        )
    if "sad" in lower or "down" in lower or "low" in lower:
        return (
            "You sound low right now. Keep the next step simple: drink water, move for a few minutes, "
            "and message someone you trust if you need support."
        )
    return (
        "I’m here with you. Tell me what feels hardest right now, or ask about your dashboard and "
        "connected platforms if you want a quick status check."
    )


async def generate_reply(
    db: AsyncSession,
    *,
    user: User,
    message: str,
    history: list[tuple[str, str]],
) -> AgentReply:
    settings = get_settings()
    if not settings.GROQ_API_KEY:
        return AgentReply(content=_fallback_response(message), provider="fallback", used_tools=[])

    tools = await _build_tools(db, user)
    llm = ChatOpenAI(
        api_key=settings.GROQ_API_KEY,
        base_url=settings.GROQ_BASE_URL,
        model=settings.GROQ_MODEL,
        temperature=settings.AGENT_TEMPERATURE,
    ).bind_tools(tools)

    messages = [SystemMessage(content=_build_system_prompt(user))]
    for role, content in history[-8:]:
        messages.append(HumanMessage(content=content) if role == "user" else AIMessage(content=content))
    messages.append(HumanMessage(content=message))

    used_tools: list[str] = []

    for _ in range(3):
        response = await llm.ainvoke(messages)
        messages.append(response)
        if not response.tool_calls:
            return AgentReply(
                content=response.content if isinstance(response.content, str) else _fallback_response(message),
                provider="groq",
                used_tools=used_tools,
            )

        for tool_call in response.tool_calls:
            selected_tool = next((tool_obj for tool_obj in tools if tool_obj.name == tool_call["name"]), None)
            if selected_tool is None:
                continue
            used_tools.append(tool_call["name"])
            tool_result = await selected_tool.ainvoke(tool_call["args"])
            messages.append(
                ToolMessage(content=str(tool_result), tool_call_id=tool_call["id"])
            )

    return AgentReply(content=_fallback_response(message), provider="fallback", used_tools=used_tools)
