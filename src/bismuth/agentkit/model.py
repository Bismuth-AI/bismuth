"""Model protocol and model-facing errors."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol, runtime_checkable

from bismuth.agentkit.messages import AssistantMessage, Message, ToolSpec


class ContextWindowExceededError(Exception):
    """Raised when a request exceeds the model's context window."""

    def __init__(self, message: str, *, context_limit: int = 0) -> None:
        super().__init__(message)
        self.context_limit = context_limit


@runtime_checkable
class ChatModel(Protocol):
    """A chat completion that may return text and tool calls.

    Implementations may stream text through ``on_text``. The returned message is
    authoritative and context overflows must raise ``ContextWindowExceededError``.
    """

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        on_text: Callable[[str], None] | None = None,
    ) -> AssistantMessage: ...
