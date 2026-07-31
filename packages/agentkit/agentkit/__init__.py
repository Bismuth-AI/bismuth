"""agentkit: a minimal, framework-free tool-using agent loop.

Provider-agnostic (depends only on a ``ChatModel`` protocol and pydantic). The core
is a loop that asks a model for a turn, runs the tools it requests through a
fail-closed permission gate, and feeds the results back until the model is done.

Standalone by design -- it must never import its host application.
"""

from __future__ import annotations

from agentkit.loop import Agent, AgentEvent, RunResult
from agentkit.messages import AssistantMessage, Message, ToolCall, ToolSpec
from agentkit.model import ChatModel
from agentkit.registry import ToolRegistry
from agentkit.subagent import subagent_tool
from agentkit.tool import FunctionTool, Permission, Tool, tool, tool_spec

__all__ = [
    "Agent",
    "AgentEvent",
    "AssistantMessage",
    "ChatModel",
    "FunctionTool",
    "Message",
    "Permission",
    "RunResult",
    "Tool",
    "ToolCall",
    "ToolRegistry",
    "ToolSpec",
    "subagent_tool",
    "tool",
    "tool_spec",
]
