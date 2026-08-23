"""The one thing the agent loop needs from an LLM: turn a transcript + tools into a turn."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol, runtime_checkable

from agentkit.messages import AssistantMessage, Message, ToolSpec


@runtime_checkable
class ChatModel(Protocol):
    """A chat completion that can request tool calls.

    Implementations adapt a provider (litellm, a fake, ...) to the neutral types.
    Given the system prompt, the transcript, and the available tools, return one
    assistant turn -- prose and/or tool calls.

    ``on_text`` receives prose as it arrives, if the implementation has it to give.
    A turn that takes twenty seconds and appears all at once reads as a hang; the same
    turn arriving a word at a time reads as thinking. Implementations that cannot stream
    ignore it, and the returned message is authoritative either way.
    """

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        on_text: Callable[[str], None] | None = None,
    ) -> AssistantMessage: ...
