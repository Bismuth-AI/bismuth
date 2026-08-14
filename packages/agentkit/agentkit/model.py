"""The one thing the agent loop needs from an LLM: turn a transcript + tools into a turn."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from agentkit.messages import AssistantMessage, Message, ToolSpec


@runtime_checkable
class ChatModel(Protocol):
    """A chat completion that can request tool calls.

    Implementations adapt a provider (litellm, a fake, ...) to the neutral types.
    Given the system prompt, the transcript, and the available tools, return one
    assistant turn -- prose and/or tool calls.
    """

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        tool_choice: str | None = None,
    ) -> AssistantMessage: ...
