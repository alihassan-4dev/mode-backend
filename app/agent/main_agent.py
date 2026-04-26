from __future__ import annotations

import json
import re
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

# Some models emit fake tool markup in plain text instead of structured tool_calls.
_LEAKED_FUNCTION_BLOCK = re.compile(
    r"<function\s*=\s*(\w+)\s*>\s*(.*?)\s*</function\s*>",
    re.DOTALL | re.IGNORECASE,
)


def _stringify_ai_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def _strip_leaked_function_blocks(text: str) -> str:
    return _LEAKED_FUNCTION_BLOCK.sub("", text).strip()


def _looks_like_user_data_question(message: str) -> bool:
    lower = message.lower()
    keywords = (
        "dashboard",
        "summary",
        "wellness",
        "mood",
        "mode",
        "stress",
        "anxiety",
        "integration",
        "facebook",
        "instagram",
        "platform",
        "profile",
        "my data",
        "my stats",
        "my report",
    )
    return any(keyword in lower for keyword in keywords)


def _sounds_vague_or_future_tense(content: str) -> bool:
    lower = content.lower()
    vague_markers = (
        "let me check",
        "i will check",
        "i'd need to check",
        "to provide personalized suggestions, i'll need",
        "i can look into that",
        "summary is not available, let me check",
    )
    return any(marker in lower for marker in vague_markers)


def extract_leaked_tool_calls(text: str) -> tuple[str, list[tuple[str, dict]]]:
    """Parse pseudo `<function=name>{args}</function>` blocks; return cleaned text and ordered unique calls."""
    calls: list[tuple[str, dict]] = []
    seen: set[tuple[str, str]] = set()

    def repl(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        raw_args = (match.group(2) or "").strip()
        args: dict = {}
        if raw_args:
            try:
                parsed = json.loads(raw_args)
                if isinstance(parsed, dict):
                    args = parsed
            except json.JSONDecodeError:
                args = {}
        key = (name, json.dumps(args, sort_keys=True))
        if key not in seen and name:
            seen.add(key)
            calls.append((name, args))
        return ""

    stripped = _LEAKED_FUNCTION_BLOCK.sub(repl, text).strip()
    return stripped, calls


@dataclass
class AgentReply:
    content: str
    provider: str
    used_tools: list[str]


def fallback_response(message: str) -> str:
    lower = message.lower()
    if "dashboard" in lower or "connect" in lower or "facebook" in lower or "instagram" in lower:
        return (
            "📊 I am here with you — let us get your dashboard ready.\n\n"
            "- 🔗 Connect Facebook or Instagram first so Mode can show real platform data.\n"
            "- ✨ Once connected, your dashboard will show true sync status and activity (not placeholder values).\n"
            "- 🌿 Your mood journey will improve as you add short daily check-ins in chat.\n"
            "- 🎯 Next step: open Integrations and connect at least one platform."
        )
    if "stress" in lower or "anx" in lower:
        return (
            "💙 Thanks for sharing — you are not alone in this.\n\n"
            "- 🌬️ Take one 60-second reset: inhale for 4, exhale for 6, repeat slowly.\n"
            "- 🎯 Pick one tiny task only and ignore the full day plan for now.\n"
            "- 📝 Therapy-style support (non-clinical): write one anxious thought, then replace it with one balanced thought.\n"
            "- 🤗 If stress keeps rising, reach out to someone you trust for support."
        )
    if "sad" in lower or "down" in lower or "low" in lower:
        return (
            "🫂 I hear you — thank you for being open.\n\n"
            "- 💧 Start with basic care: drink water and take a short 5-minute walk.\n"
            "- ✨ Choose one simple win you can finish in under 10 minutes.\n"
            "- 📝 Therapy-style support (non-clinical): try a quick mood log with 3 lines — feeling, trigger, next kind action.\n"
            "- 💙 If this feels heavy for long periods, consider talking with a licensed mental health professional."
        )
    return (
        "✨ I am here for you.\n\n"
        "- 💬 Tell me what feels hardest right now, and I will break it into small steps.\n"
        "- 📊 I can also give a quick dashboard and integrations status check.\n"
        "- 🌿 If you want, I can suggest one gentle therapy-style coping exercise (non-clinical)."
    )


async def _humanize_reply_after_leaked_tools(
    *,
    settings,
    user: User,
    user_message: str,
    partial_assistant_text: str,
    tool_context: str,
) -> str:
    """One-shot natural reply using verified tool output; LLM is not tool-bound to avoid another fake markup turn."""
    display_name = user.full_name or user.email.split("@")[0]
    humanize_llm = ChatOpenAI(
        api_key=settings.GROQ_API_KEY,
        base_url=settings.GROQ_BASE_URL,
        model=settings.AGENT_GROQ_MODEL or settings.GROQ_MODEL,
        temperature=min(0.45, float(settings.AGENT_TEMPERATURE) + 0.12),
    )
    system = (
        build_system_prompt(user)
        + "\n\nImportant: You are writing the final user-visible message only. "
        "Do not describe retrieving data, calling tools, APIs, functions, or 'wellness summary' as a mechanism. "
        "Never output `<function=`, `</function>`, JSON tool payloads, or raw tool names. "
        f"Use the facts below to personalize advice for {display_name} in warm Markdown bullets (3-6), with emojis."
    )
    human = HumanMessage(
        content=(
            f"What the user wrote:\n{user_message}\n\n"
            f"Optional earlier wording (may be empty; do not repeat technical phrasing):\n{partial_assistant_text.strip()}\n\n"
            f"Verified facts from their Mode account (use only this; do not invent numbers):\n{tool_context}\n\n"
            "Write your reply now: supportive, concrete, and personal. If mood or stress appears, add one "
            "non-clinical coping idea (breathing, grounding, journaling, or a tiny next action)."
        )
    )
    out = await humanize_llm.ainvoke([SystemMessage(content=system), human])
    text = _stringify_ai_content(out.content).strip()
    text = _strip_leaked_function_blocks(text)
    return text


async def _build_forced_tool_context(
    *,
    tool_map: dict[str, object],
    message: str,
    used_tools: list[str],
) -> str:
    tool_context_parts: list[str] = []

    summary_tool = tool_map.get("get_wellness_summary")
    if summary_tool is not None:
        used_tools.append("get_wellness_summary")
        summary = await summary_tool.ainvoke({"include_recommendations": True})
        tool_context_parts.append(f"## get_wellness_summary\n{summary}")

    user_context_tool = tool_map.get("get_current_user_context")
    if user_context_tool is not None:
        used_tools.append("get_current_user_context")
        user_context = await user_context_tool.ainvoke({})
        tool_context_parts.append(f"## get_current_user_context\n{user_context}")

    lower_message = message.lower()
    if any(keyword in lower_message for keyword in ("mode", "dashboard", "state")):
        mode_tool = tool_map.get("explain_user_mode")
        if mode_tool is not None:
            used_tools.append("explain_user_mode")
            mode_summary = await mode_tool.ainvoke({"detail_level": "short"})
            tool_context_parts.append(f"## explain_user_mode\n{mode_summary}")

    return "\n\n".join(tool_context_parts)


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
    used_tools = list(result.get("used_tools", []))

    raw = _stringify_ai_content(getattr(final_message, "content", "")).strip()
    if not raw:
        raw = fallback_response(message)

    partial_clean, leaked_calls = extract_leaked_tool_calls(raw)
    content = partial_clean if partial_clean else _strip_leaked_function_blocks(raw)

    if leaked_calls:
        tool_context_parts: list[str] = []
        for name, args in leaked_calls:
            selected = tool_map.get(name)
            if selected is None:
                continue
            used_tools.append(name)
            result_text = await selected.ainvoke(args)
            tool_context_parts.append(f"## {name}\n{result_text}")
        tool_context = "\n\n".join(tool_context_parts)
        if tool_context:
            humanized = await _humanize_reply_after_leaked_tools(
                settings=settings,
                user=user,
                user_message=message,
                partial_assistant_text=content,
                tool_context=tool_context,
            )
            content = humanized or content
        else:
            content = content or _strip_leaked_function_blocks(raw)

    needs_forced_context = (not used_tools and _looks_like_user_data_question(message)) or _sounds_vague_or_future_tense(content)
    if needs_forced_context:
        tool_context = await _build_forced_tool_context(tool_map=tool_map, message=message, used_tools=used_tools)
        if tool_context:
            humanized = await _humanize_reply_after_leaked_tools(
                settings=settings,
                user=user,
                user_message=message,
                partial_assistant_text=content,
                tool_context=tool_context,
            )
            content = humanized or content

    content = _strip_leaked_function_blocks(content)
    if "<function" in content.lower():
        content = re.sub(r"<function\s*=\s*\w+\s*>.*?</function\s*>", "", content, flags=re.DOTALL | re.IGNORECASE).strip()

    if not content:
        content = fallback_response(message)

    return AgentReply(content=content, provider="groq-langgraph", used_tools=used_tools)
