from __future__ import annotations

from dataclasses import dataclass

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.prompt import build_system_prompt
from app.agent.state import ModeAgentState
from app.agent.tools import build_agent_tools
from app.core.config import get_settings
from app.models.user import User


@dataclass
class AgentReply:
    content: str
    provider: str
    used_tools: list[str]


def fallback_response(message: str) -> str:
    lower = message.lower()
    if "dashboard" in lower or "connect" in lower or "facebook" in lower or "instagram" in lower:
        return (
            "I can help with your Mode dashboard. Connect Facebook or Instagram first, then the dashboard will show "
            "real platform state and real mood history from your chat check-ins instead of fake history."
        )
    if "stress" in lower or "anx" in lower:
        return (
            "Your message sounds stress-related. Start with one small reset: pause, breathe slowly for 60 seconds, "
            "and decide on the next single action instead of the whole day."
        )
    if "sad" in lower or "down" in lower or "low" in lower:
        return (
            "You sound low right now. Keep the next step simple: drink water, move for a few minutes, and message "
            "someone you trust if you need support."
        )
    return (
        "I'm here with you. Tell me what feels hardest right now, or ask about your dashboard and connected platforms "
        "if you want a quick status check."
    )


async def run_agent(
    db: AsyncSession,
    *,
    user: User,
    message: str,
    history: list[tuple[str, str]],
) -> AgentReply:
    settings = get_settings()
    if not settings.GROQ_API_KEY:
        return AgentReply(content=fallback_response(message), provider="fallback", used_tools=[])

    tools = await build_agent_tools(db, user)
    tool_map = {tool_obj.name: tool_obj for tool_obj in tools}
    llm = ChatOpenAI(
        api_key=settings.GROQ_API_KEY,
        base_url=settings.GROQ_BASE_URL,
        model=settings.AGENT_GROQ_MODEL or settings.GROQ_MODEL,
        temperature=settings.AGENT_TEMPERATURE,
    ).bind_tools(tools)

    async def call_model(state: ModeAgentState) -> ModeAgentState:
        response = await llm.ainvoke(state["messages"])
        return {"messages": [response], "used_tools": state.get("used_tools", [])}

    async def call_tools(state: ModeAgentState) -> ModeAgentState:
        last_message = state["messages"][-1]
        used_tools = list(state.get("used_tools", []))
        tool_messages: list[ToolMessage] = []

        for tool_call in getattr(last_message, "tool_calls", []) or []:
            selected_tool = tool_map.get(tool_call["name"])
            if selected_tool is None:
                continue
            used_tools.append(tool_call["name"])
            result = await selected_tool.ainvoke(tool_call["args"])
            tool_messages.append(ToolMessage(content=str(result), tool_call_id=tool_call["id"]))

        return {"messages": tool_messages, "used_tools": used_tools}

    def should_continue(state: ModeAgentState) -> str:
        last_message = state["messages"][-1]
        if getattr(last_message, "tool_calls", None):
            return "tools"
        return END

    graph = StateGraph(ModeAgentState)
    graph.add_node("agent", call_model)
    graph.add_node("tools", call_tools)
    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")
    compiled = graph.compile()

    messages = [SystemMessage(content=build_system_prompt(user))]
    for role, content in history[-8:]:
        messages.append(HumanMessage(content=content) if role == "user" else AIMessage(content=content))
    messages.append(HumanMessage(content=message))

    result = await compiled.ainvoke({"messages": messages, "used_tools": []}, {"recursion_limit": 8})
    final_message = result["messages"][-1]
    content = final_message.content if isinstance(final_message.content, str) else fallback_response(message)
    return AgentReply(content=content, provider="groq-langgraph", used_tools=result.get("used_tools", []))
