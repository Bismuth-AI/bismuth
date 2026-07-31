"""The agent loop: call the model, run the tools it asks for, feed results back, repeat."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field

from pydantic import BaseModel, ValidationError

from agentkit.messages import Message, ToolCall
from agentkit.model import ChatModel
from agentkit.registry import ToolRegistry
from agentkit.tool import Permission, Tool

OnAsk = Callable[[Tool, BaseModel], Awaitable[Permission]]
OnEvent = Callable[["AgentEvent"], None]


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """One step, for logging or debugging a run.

    kind is one of: ``turn``, ``tool_call``, ``tool_result``, ``tool_denied``,
    ``tool_error``, ``stop``.
    """

    kind: str
    data: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class RunResult:
    """The outcome of a run: the final answer plus the full trace."""

    text: str
    messages: list[Message]
    events: list[AgentEvent]
    turns: int
    stopped: str  # "final" (model finished) | "max_turns" (guard tripped)


class Agent:
    """A tool-using agent over one ChatModel and a fixed tool set.

    The loop is: ask the model for a turn; if it requested tools, run each through
    the permission gate and feed the results back; stop when a turn has no tool
    calls. Mutating tools return ``Permission.ASK`` and only run if ``on_ask``
    approves -- otherwise the model is told to propose the action, not take it.
    """

    def __init__(
        self,
        *,
        model: ChatModel,
        tools: Iterable[Tool] | ToolRegistry,
        system: str,
        max_turns: int = 24,
        on_ask: OnAsk | None = None,
        on_event: OnEvent | None = None,
    ) -> None:
        self._model = model
        self._registry = tools if isinstance(tools, ToolRegistry) else ToolRegistry(tools)
        self._system = system
        self._max_turns = max_turns
        self._on_ask = on_ask
        self._on_event = on_event

    async def run(self, user_input: str, *, on_event: OnEvent | None = None) -> RunResult:
        """Run to completion. ``on_event`` overrides the instance sink for this run."""
        messages: list[Message] = [Message("user", user_input)]
        events: list[AgentEvent] = []
        sink = on_event if on_event is not None else self._on_event

        def emit(kind: str, **data: object) -> None:
            event = AgentEvent(kind, data)
            events.append(event)
            if sink is not None:
                sink(event)

        last_text = ""
        for turn in range(1, self._max_turns + 1):
            reply = await self._model.complete(
                system=self._system, messages=messages, tools=self._registry.specs()
            )
            last_text = reply.text
            messages.append(Message("assistant", reply.text, reply.tool_calls))
            emit("turn", index=turn, text=reply.text, tools=[c.name for c in reply.tool_calls])

            if not reply.tool_calls:
                emit("stop", reason="final")
                return RunResult(reply.text, messages, events, turn, "final")

            for call in reply.tool_calls:
                emit("tool_call", id=call.id, name=call.name, arguments=call.arguments)
            for call, (content, kind) in zip(
                reply.tool_calls, await self._dispatch_calls(reply.tool_calls), strict=True
            ):
                messages.append(Message("tool", content, tool_call_id=call.id))
                emit(kind, id=call.id, name=call.name, preview=content[:200])

        emit("stop", reason="max_turns")
        return RunResult(last_text, messages, events, self._max_turns, "max_turns")

    async def _dispatch_calls(self, calls: tuple[ToolCall, ...]) -> list[tuple[str, str]]:
        """Run a turn's tool calls -- in parallel when every one is concurrency-safe."""
        safe = all(
            getattr(self._registry.get(c.name), "concurrency_safe", False) for c in calls
        )
        if safe and len(calls) > 1:
            return list(await asyncio.gather(*(self._dispatch(c) for c in calls)))
        return [await self._dispatch(c) for c in calls]

    async def _dispatch(self, call: ToolCall) -> tuple[str, str]:
        """Run one tool call through parse -> permission -> execute. Returns (content, event_kind)."""
        tool = self._registry.get(call.name)
        if tool is None:
            return f"Error: no tool named '{call.name}'.", "tool_error"

        try:
            args = tool.params.model_validate(call.arguments)
        except ValidationError as exc:
            return f"Error: invalid arguments for '{call.name}': {exc}", "tool_error"

        decision = tool.permission(args)
        if decision is Permission.ASK:
            decision = await self._on_ask(tool, args) if self._on_ask is not None else Permission.DENY
        if decision is not Permission.ALLOW:
            return (
                f"Not allowed: '{call.name}' needs user approval. "
                f"Propose it to the user instead of calling it.",
                "tool_denied",
            )

        try:
            return await tool.run(args), "tool_result"
        except Exception as exc:  # a tool failing must not crash the loop
            return f"Error running '{call.name}': {exc}", "tool_error"
