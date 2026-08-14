"""A scripted ChatModel for tests and offline runs."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from agentkit.messages import AssistantMessage, Message, ToolCall, ToolSpec

Handler = Callable[[str, list[Message], list[ToolSpec]], AssistantMessage]


class FakeModel:
    """Returns canned turns, or computes them from the transcript, and records calls."""

    def __init__(
        self,
        turns: Sequence[AssistantMessage] | None = None,
        *,
        handler: Handler | None = None,
    ) -> None:
        if turns is None and handler is None:
            raise ValueError("FakeModel needs either turns or a handler")
        self._turns = list(turns or [])
        self._handler = handler
        self.calls: list[tuple[str, list[Message], list[ToolSpec]]] = []

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        tool_choice: str | None = None,
    ) -> AssistantMessage:
        del tool_choice
        self.calls.append((system, list(messages), list(tools)))
        if self._handler is not None:
            return self._handler(system, list(messages), list(tools))
        if self._turns:
            return self._turns.pop(0)
        raise AssertionError("FakeModel ran out of scripted turns")


def call(name: str, arguments: dict[str, object] | None = None, *, call_id: str = "c1") -> ToolCall:
    """Build a ToolCall for scripting a turn."""
    return ToolCall(id=call_id, name=name, arguments=arguments or {})


def says(text: str = "", *calls: ToolCall) -> AssistantMessage:
    """Build an assistant turn: some text and/or tool calls."""
    return AssistantMessage(text=text, tool_calls=tuple(calls))
