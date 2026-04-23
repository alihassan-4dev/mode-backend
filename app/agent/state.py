from __future__ import annotations

from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class ModeAgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    used_tools: list[str]
