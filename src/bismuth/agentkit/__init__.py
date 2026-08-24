"""A provider-neutral, tool-using agent loop."""

from __future__ import annotations

from bismuth.agentkit import budget
from bismuth.agentkit.loop import Agent, AgentEvent, RunResult
from bismuth.agentkit.messages import AssistantMessage, Message, ToolCall, ToolSpec
from bismuth.agentkit.model import ChatModel, ContextWindowExceededError
from bismuth.agentkit.registry import ToolRegistry
from bismuth.agentkit.subagent import subagent_tool
from bismuth.agentkit.tool import FunctionTool, Permission, Tool, tool, tool_spec

__all__ = [
    "Agent",
    "AgentEvent",
    "AssistantMessage",
    "ChatModel",
    "ContextWindowExceededError",
    "FunctionTool",
    "Message",
    "Permission",
    "RunResult",
    "Tool",
    "ToolCall",
    "ToolRegistry",
    "ToolSpec",
    "budget",
    "subagent_tool",
    "tool",
    "tool_spec",
]
