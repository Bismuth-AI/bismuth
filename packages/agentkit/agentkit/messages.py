"""Provider-neutral conversation types. A ChatModel adapter maps these to/from its wire format."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A model's request to invoke one tool."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AssistantMessage:
    """One model turn: prose plus any tool calls it wants run before continuing."""

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    input_tokens: int = 0
    """What the provider counted for the request that produced this turn.

    Zero when the provider does not say. A real number here anchors the context
    budget to something measured instead of estimated -- see ``budget.since``."""


@dataclass(frozen=True, slots=True)
class Message:
    """One entry in the running transcript sent to the model."""

    role: str  # "user" | "assistant" | "tool"
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None  # set on a "tool" result, echoing the call it answers


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """What the model is told about one tool: its name, purpose, and JSON-Schema params."""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
