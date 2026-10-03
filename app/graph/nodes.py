from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage, AIMessage
from langchain_groq import ChatGroq
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from config import settings, logger
from tools import ALL_TOOLS


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


tool_node = ToolNode(ALL_TOOLS)

model = ChatGroq(
    model=settings.GROQ_MODEL,
    groq_api_key=settings.GROQ_API_KEY,
    temperature=0,
    timeout=30,
    max_retries=2,
).bind_tools(ALL_TOOLS)


def call_model(state: AgentState) -> dict:
    logger.debug("Invoking LLM with %d messages.", len(state["messages"]))
    response = model.invoke(state["messages"])
    return {"messages": [response]}


def should_continue(state: AgentState) -> str:
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return "__end__"
